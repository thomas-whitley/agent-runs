"""The worker role: claim a pending run, execute its loop, append its events."""

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
from opentelemetry import trace

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
from app.migrations import apply_migrations
from app.model import Model, StubModel
from app.pagespeed import run_pagespeed
from app.retrieval import Retriever, build_retriever, index_corpus
from app.runs import claim_run, finish_run, heartbeat, record_step
from app.tasks import CLOUD_FALLBACK_CHECK_KINDS, TASK_TYPES
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.worker")

# pytest is the only loop type with an executor so far, and a site_check the
# worker takes over runs on PageSpeed in run_cloud_check. Every other
# registered type is refused, closing its stream, until its own step lands.
_RUNNABLE_TYPES = {"pytest"}

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
) -> LoopResult | None:
    """Execute one claimed run. None means it was refused rather than run.

    Building the model happens here, not before the run is claimed, so a
    provider missing its credentials refuses the one run that needed it
    instead of leaving it claimed and running with no way to close its
    stream.
    """
    task_type_name = _run_type(conn, run_id)
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

    return run_agent_loop(
        conn,
        run_id,
        model,
        token_budget=settings.token_budget,
        verify_timeout_seconds=settings.verify_timeout_seconds,
        on_step=lambda: heartbeat(conn, run_id, settings.worker_id),
        retriever=retriever,
        worker_id=settings.worker_id,
        tracer=tracer,
        provider=task_type.provider,
    )


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    # No FastAPI app here to instrument; this turns on the same global tracer
    # provider the loop's step spans pick up ambiently.
    configure_telemetry()
    retriever = build_retriever(settings)
    models: dict[str, Model] = {}

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
                process_run(conn, run_id, settings, retriever, model_builder=cached_model_builder)
            except Exception:
                # run_agent_loop already closes a run it could not finish. This
                # catches everything outside it, so the worker outlives a blip.
                logger.exception("worker loop error, carrying on")
                time.sleep(settings.poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
