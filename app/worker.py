"""The worker role: claim a pending run, execute its loop, append its events."""

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
from opentelemetry import trace

from app.budget import BudgetTrip, check_budget
from app.chat import run_chat
from app.config import (
    DEFAULT_CHECK_CLAIM_WINDOW_SECONDS,
    DEFAULT_LEASE_SECONDS,
    PROVIDERS,
    Settings,
    load_settings,
)
from app.corpus import load_corpus
from app.logging_setup import configure_logging
from app.loop import LoopResult, run_agent_loop
from app.mercury_config import MercuryConfig, RepoConfig, load_mercury_config
from app.migrations import apply_migrations
from app.model import Model, StubModel
from app.pagespeed import run_pagespeed
from app.progress import push_progress
from app.repo_chore import ChoreSetup, GitHubClient, run_repo_chore
from app.retrieval import Retriever, build_retriever, index_corpus
from app.runs import claim_run, finish_run, heartbeat, record_step
from app.tasks import CLOUD_FALLBACK_CHECK_KINDS, TASK_TYPES
from app.telegram import TelegramClient, TelegramError, telegram_client
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.worker")

# pytest runs the agent loop and chat turns a Telegram message into a task
# (app/chat.py). A site_check the worker takes over runs on PageSpeed in
# run_cloud_check. Every other
# registered type is refused, closing its stream, until its own step lands.
_RUNNABLE_TYPES = {"pytest", "chat", "repo_chore"}

# Unclaimed runs, and runs whose worker stopped reporting for longer than the
# lease. The second case is a worker that was killed outright.
#
# A site_check is claimed here only as the cloud fallback. A lighthouse check
# waits the claim window for the self hosted worker. A lease that lapses puts
# it back to waiting, so the window then counts from the moment the lease ran
# out. broken_links never falls back, because PageSpeed cannot crawl. Uptime
# checks are never claimed here: the scheduler creates and closes them itself.
_CLAIMABLE = """
SELECT id FROM runs
WHERE finished_at IS NULL
  AND (
        (
          type <> 'site_check'
          AND (
                (status = 'pending' AND claimed_by IS NULL)
                OR (status = 'running' AND heartbeat_at < now() - make_interval(secs => %(lease)s))
              )
        )
        OR (
          type = 'site_check'
          AND check_kind = ANY(%(cloud_kinds)s)
          AND (
                (claimed_by IS NULL AND created_at < now() - make_interval(secs => %(window)s))
                OR (
                  status = 'running'
                  AND heartbeat_at < now() - make_interval(secs => %(lease)s + %(window)s)
                )
              )
        )
      )
ORDER BY created_at
LIMIT 5
"""

# site_check makes no model call, and the checks worker claims it too, so it
# is left out of the limit that protects the model key.
_STARTED_TODAY = """
SELECT count(*) FROM runs
WHERE claimed_by IS NOT NULL
  AND type <> 'site_check'
  AND created_at >= date_trunc('day', now())
"""


def runs_started_today(conn: psycopg.Connection) -> int:
    return conn.execute(_STARTED_TODAY).fetchone()[0]


def claim_next_run(
    conn: psycopg.Connection,
    worker_id: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    check_claim_window_seconds: float = DEFAULT_CHECK_CLAIM_WINDOW_SECONDS,
) -> str | None:
    """Claim the oldest claimable run. None means there is nothing to do."""
    parameters = {
        "lease": lease_seconds,
        "window": check_claim_window_seconds,
        "cloud_kinds": list(CLOUD_FALLBACK_CHECK_KINDS),
    }
    for (run_id,) in conn.execute(_CLAIMABLE, parameters).fetchall():
        if claim_run(conn, run_id, worker_id, lease_seconds=lease_seconds):
            return run_id
    return None


def refuse_run(conn: psycopg.Connection, run_id: str, reason: str) -> None:
    """Close a run the worker will not execute, so its stream ends rather than hanging."""
    next_seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM events WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    record_step(conn, run_id, next_seq, "done", output={"status": "refused", "reason": reason})
    conn.execute("UPDATE runs SET status = 'refused', finished_at = now() WHERE id = %s", (run_id,))


def _run_type(conn: psycopg.Connection, run_id: str) -> str:
    return conn.execute("SELECT type FROM runs WHERE id = %s", (run_id,)).fetchone()[0]


def run_cloud_check(
    conn: psycopg.Connection,
    run_id: str,
    settings: Settings,
    check_runner: Callable[[str, str | None], dict[str, Any]] = run_pagespeed,
) -> LoopResult | None:
    """Run a lighthouse check the worker took over on PageSpeed, and close it
    the way POST /checks/{id}/result does: the result as step 1, a done event
    as step 2, succeeded whatever the result says. None means another worker
    took the check during the call, so its result was dropped."""
    url = conn.execute("SELECT task FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
    fields = {"run_id": run_id, "worker_id": settings.worker_id}
    try:
        result = check_runner(url, settings.pagespeed_api_key)
    except Exception as error:
        # Any failure is the check's finding, as it is for the checks worker.
        logger.error("pagespeed failed for check %s: %s", run_id, error, extra=fields)
        result = {"error": str(error)}

    done = {"status": "succeeded"}
    with conn.transaction():
        if not finish_run(conn, run_id, "succeeded", 0, worker_id=settings.worker_id):
            logger.warning(
                "dropped the result of check %s, which another worker holds", run_id, extra=fields
            )
            return None
        record_step(conn, run_id, 1, "check", output=result, worker_id=settings.worker_id)
        record_step(conn, run_id, 2, "done", output=done, worker_id=settings.worker_id)
    logger.info("check %s closed on pagespeed", run_id, extra=fields)
    return LoopResult(status="succeeded", attempts=1, tokens_used=0)


def build_model(settings: Settings, provider_name: str) -> Model:
    """Build the model for one registered provider. MODEL=stub skips the registry."""
    if settings.model == "stub":
        return StubModel(replies=[""])

    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise RuntimeError(f"no provider registered as {provider_name!r}")

    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise RuntimeError(f"no model credentials: set {provider.api_key_env}")

    if provider.kind == "anthropic":
        from app.model import AnthropicModel

        return AnthropicModel(
            model=provider.model,
            api_key=api_key,
            timeout_seconds=settings.model_timeout_seconds,
        )

    from app.model import OpenAICompatibleModel

    return OpenAICompatibleModel(
        model=provider.model,
        api_key=api_key,
        base_url=provider.base_url,
        timeout_seconds=settings.model_timeout_seconds,
    )


def process_run(
    conn: psycopg.Connection,
    run_id: str,
    settings: Settings,
    retriever: Retriever | None = None,
    tracer: trace.Tracer | None = None,
    model_builder: Callable[[Settings, str], Model] = build_model,
    check_runner: Callable[[str, str | None], dict[str, Any]] = run_pagespeed,
    owner_chat_id: int | None = None,
    repos: tuple[RepoConfig, ...] = (),
) -> LoopResult | None:
    """Execute one claimed run. None means it was refused rather than run.

    Building the model happens here, not before the run is claimed, so a
    provider missing its credentials refuses the one run that needed it
    instead of leaving it claimed and running with no way to close its
    stream.
    """
    task_type_name = _run_type(conn, run_id)
    telegram = telegram_client(settings)
    try:
        result = _process_run(
            conn,
            run_id,
            task_type_name,
            settings,
            retriever,
            tracer,
            model_builder,
            check_runner,
            telegram,
            owner_chat_id,
            repos,
        )
        if result is not None and result.status == "budget_exhausted":
            _tell_owner(
                telegram,
                owner_chat_id,
                f"Budget: run {run_id[:8]} stopped at its own budget of "
                f"{settings.token_budget:,} tokens.",
            )
        return result
    finally:
        # The last word on a run started from Telegram, whichever way it ended.
        # A chat run edits its own message instead.
        if task_type_name != "chat":
            push_progress(conn, run_id, telegram)


def _process_run(
    conn: psycopg.Connection,
    run_id: str,
    task_type_name: str,
    settings: Settings,
    retriever: Retriever | None,
    tracer: trace.Tracer | None,
    model_builder: Callable[[Settings, str], Model],
    check_runner: Callable[[str, str | None], dict[str, Any]],
    telegram: TelegramClient | None,
    owner_chat_id: int | None,
    repos: tuple[RepoConfig, ...],
) -> LoopResult | None:
    # A check makes no model call, so the daily limit that protects the key
    # does not apply to it.
    if task_type_name == "site_check":
        return run_cloud_check(conn, run_id, settings, check_runner)

    # Check then act, which is safe only because the worker runs at one replica
    # (maxReplicas is 1 in the Bicep). Two workers could both pass this.
    if runs_started_today(conn) > settings.max_runs_per_day:
        logger.warning(
            "refusing run %s: daily limit of %s reached",
            run_id,
            settings.max_runs_per_day,
            extra={"run_id": run_id, "worker_id": settings.worker_id},
        )
        refuse_run(conn, run_id, f"daily limit of {settings.max_runs_per_day} runs reached")
        return None

    if task_type_name not in _RUNNABLE_TYPES:
        logger.warning(
            "refusing run %s: task type %s has no executor yet",
            run_id,
            task_type_name,
            extra={"run_id": run_id, "worker_id": settings.worker_id},
        )
        refuse_run(conn, run_id, f"task type {task_type_name!r} is not runnable yet")
        return None

    task_type = TASK_TYPES[task_type_name]
    trip = check_budget(
        conn, task_type.provider, settings.daily_tokens_per_provider, settings.monthly_budget_usd
    )
    if trip is not None:
        end_on_budget(conn, run_id, trip)
        _tell_owner(telegram, owner_chat_id, f"Budget: run {run_id[:8]} stopped. {trip.message}")
        return None

    try:
        model = model_builder(settings, task_type.provider)
    except RuntimeError as error:
        logger.error(
            "refusing run %s: %s",
            run_id,
            error,
            extra={"run_id": run_id, "worker_id": settings.worker_id},
        )
        refuse_run(conn, run_id, str(error))
        return None

    if task_type_name == "repo_chore":
        return _run_chore(conn, run_id, model, settings, repos, telegram, task_type.budget_tokens)

    if task_type_name == "chat":
        tokens = run_chat(conn, run_id, model, telegram, worker_id=settings.worker_id, repos=repos)
        return LoopResult(status="succeeded", attempts=1, tokens_used=tokens)

    return run_agent_loop(
        conn,
        run_id,
        model,
        token_budget=settings.token_budget,
        verify_timeout_seconds=settings.verify_timeout_seconds,
        on_step=lambda: _step_landed(conn, run_id, settings.worker_id, telegram),
        retriever=retriever,
        worker_id=settings.worker_id,
        tracer=tracer,
        provider=task_type.provider,
    )


def _run_chore(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    settings: Settings,
    repos: tuple[RepoConfig, ...],
    telegram: TelegramClient | None,
    token_budget: int,
) -> LoopResult | None:
    name = conn.execute("SELECT repo FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
    repo = next((r for r in repos if r.name == name and r.test_command), None)
    if repo is None:
        # Approved while it was listed, and taken out of mercury.yaml since.
        refuse_run(conn, run_id, f"{name} is not a repo with a test_command in mercury.yaml")
        return None
    setup = ChoreSetup(
        repo=repo,
        clone_base=settings.github_clone_base,
        github=GitHubClient(settings.mercury_github_token, settings.github_api_url),
        token=settings.mercury_github_token,
        test_timeout_seconds=settings.repo_test_timeout_seconds,
        heartbeat_database_url=settings.database_url,
    )
    return run_repo_chore(
        conn,
        run_id,
        model,
        setup,
        worker_id=settings.worker_id,
        token_budget=token_budget,
        on_step=lambda: _step_landed(conn, run_id, settings.worker_id, telegram),
    )


def end_on_budget(conn: psycopg.Connection, run_id: str, trip: BudgetTrip) -> None:
    """Close a run a cap stopped, with one done event that names the cap."""
    logger.warning("run %s stopped by the %s cap", run_id, trip.cap, extra={"run_id": run_id})
    output = {"status": "budget", "cap": trip.cap, "reason": trip.message}
    next_seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM events WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    record_step(conn, run_id, next_seq, "done", output=output)
    conn.execute("UPDATE runs SET status = 'budget', finished_at = now() WHERE id = %s", (run_id,))


def _tell_owner(telegram: TelegramClient | None, chat_id: int | None, text: str) -> None:
    if telegram is None or chat_id is None:
        return
    try:
        telegram.send_message(chat_id, text)
    except TelegramError as error:
        logger.error("budget message not sent: %s", error)


def _step_landed(
    conn: psycopg.Connection, run_id: str, worker_id: str, telegram: TelegramClient | None
) -> bool:
    """Keep the lease, and show the step on Telegram if the run came from there."""
    held = heartbeat(conn, run_id, worker_id)
    if held:
        push_progress(conn, run_id, telegram)
    return held


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    # No FastAPI app here to instrument; this turns on the same global tracer
    # provider the loop's step spans pick up ambiently.
    configure_telemetry()
    retriever = build_retriever(settings)
    models: dict[str, Model] = {}
    # The owner's chat, told when a cap stops a run, and the repos a chore
    # may touch. No config, no message and no chores.
    try:
        mercury = load_mercury_config(settings.mercury_config_path)
    except FileNotFoundError:
        mercury = MercuryConfig(sites=())
    owner_chat_id = mercury.telegram_chat_id

    def cached_model_builder(settings: Settings, provider_name: str) -> Model:
        """One client per provider, built the first time a run needs it."""
        if provider_name not in models:
            models[provider_name] = build_model(settings, provider_name)
        return models[provider_name]

    logger.info(
        "worker %s started, model %s, retrieval by %s",
        settings.worker_id,
        settings.model,
        retriever.name,
        extra={"worker_id": settings.worker_id},
    )

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        added = index_corpus(conn, load_corpus(), embedder=retriever.embedder)
        logger.info("corpus indexed, %s new chunks", added, extra={"worker_id": settings.worker_id})

        while True:
            try:
                run_id = claim_next_run(
                    conn,
                    settings.worker_id,
                    settings.lease_seconds,
                    settings.check_claim_window_seconds,
                )
                if run_id is None:
                    time.sleep(settings.poll_seconds)
                    continue
                logger.info(
                    "claimed run %s",
                    run_id,
                    extra={"run_id": run_id, "worker_id": settings.worker_id},
                )
                process_run(
                    conn,
                    run_id,
                    settings,
                    retriever,
                    model_builder=cached_model_builder,
                    owner_chat_id=owner_chat_id,
                    repos=mercury.repos,
                )
            except Exception:
                # run_agent_loop already closes a run it could not finish. This
                # catches everything outside it, so the worker outlives a blip.
                logger.exception("worker loop error, carrying on")
                time.sleep(settings.poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
