"""ROLE=scheduler: a Container Apps Job that wakes on a cron trigger, checks
the configured sites, creates any weekly browser check that is due, and
exits. It creates each uptime run through the API with the bearer token, so
the run exists with no client attached to watch it, then executes and closes
the run itself in the same process. A plain HTTP check needs no browser and
no separate worker, and the agent loop worker never claims uptime runs (see
_CLAIMABLE in app/worker.py).

The weekly lighthouse and broken_links checks are only created here. The
self hosted checks worker claims them, and a lighthouse check nobody claims
within the window falls back to PageSpeed on the Python worker.
"""

import json
import logging
import urllib.request

import psycopg
from opentelemetry import trace

from app.approvals import ask, expire_due
from app.checks import check_site
from app.config import load_settings
from app.logging_setup import configure_logging
from app.mercury_config import load_mercury_config
from app.migrations import apply_migrations
from app.runs import finish_run, record_step
from app.schedule_state import SUSPEND_AFTER, is_suspended, record_failure, record_success
from app.tasks import SELF_HOSTED_CHECK_KINDS
from app.telegram import TelegramClient, TelegramError, telegram_client
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.scheduler")

# The api scales to zero, and an hourly Job almost always finds it there.
# Container Apps holds the first request while a replica starts, so this has
# to cover a cold start, not only a request. The Job's replicaTimeout is 300.
CREATE_RUN_TIMEOUT_SECONDS = 60.0

# A weekly check is due when none of its kind for its page was created in
# this long. Counting from the last one, rather than reading a cron day,
# means an hourly Job that missed its slot catches up on the next run.
WEEKLY_INTERVAL = "7 days"

_LAST_CHECK_IS_RECENT = """
SELECT EXISTS (
    SELECT 1 FROM runs
    WHERE type = 'site_check' AND check_kind = %s AND task = %s
      AND created_at > now() - %s::interval
)
"""


def _create_run(
    api_base_url: str,
    bearer_token: str,
    url: str,
    timeout_seconds: float = CREATE_RUN_TIMEOUT_SECONDS,
    kind: str | None = None,
) -> str:
    inputs = {"task": url} if kind is None else {"task": url, "kind": kind}
    body = json.dumps({"type": "site_check", "inputs": inputs}).encode()
    request = urllib.request.Request(
        f"{api_base_url}/runs",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())["id"]


def run_due_checks(
    conn: psycopg.Connection,
    sites: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> list[str]:
    """Check every configured site not currently suspended. Returns the ids
    of the runs created, so a caller (or a test) can inspect them. With a
    Telegram client and chat, the failure that suspends a site sends one
    message with a Resume button."""
    created: list[str] = []

    for url in sites:
        schedule_name = f"site_uptime:{url}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue

        # OSError covers URLError, HTTPError (a 401 from a wrong token) and a
        # timeout while reading the response. It counts as a failure, so a bad
        # token or a dead api suspends the schedule rather than skipping it
        # every hour with nothing to show that checks have stopped.
        try:
            run_id = _create_run(api_base_url, bearer_token, url)
        except OSError:
            logger.exception("could not create a run for %s, counted as a failure", url)
            _fail(conn, schedule_name, url, telegram, chat_id)
            continue

        logger.info("scheduled run %s created with no client", run_id, extra={"run_id": run_id})
        created.append(run_id)

        result = check_site(url)
        record_step(
            conn,
            run_id,
            1,
            "check",
            output={
                "status_code": result.status_code,
                "latency_ms": result.latency_ms,
                "passed": result.passed,
                "error": result.error,
            },
        )
        finish_run(conn, run_id, "succeeded" if result.passed else "failed", 0)

        if result.passed:
            record_success(conn, schedule_name)
        else:
            _fail(conn, schedule_name, url, telegram, chat_id)

    return created


def _fail(
    conn: psycopg.Connection,
    schedule_name: str,
    url: str,
    telegram: TelegramClient | None,
    chat_id: int | None,
) -> None:
    if not record_failure(conn, schedule_name) or telegram is None or chat_id is None:
        return
    text = f"{url} failed {SUSPEND_AFTER} checks in a row, so its hourly check is suspended."
    try:
        ask(conn, telegram, chat_id, "resume_schedule", text, schedule_name=schedule_name)
    except TelegramError as error:
        # The suspension stands and /status shows it; only the message is lost.
        logger.error("could not send the suspension message: %s", error)


def schedule_weekly_checks(
    conn: psycopg.Connection,
    pages: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
) -> list[str]:
    """Create a lighthouse and a broken_links check for every page that has
    not had one of that kind this week. Returns the ids of the runs created."""
    created: list[str] = []

    for url in pages:
        for kind in SELF_HOSTED_CHECK_KINDS:
            recent = conn.execute(_LAST_CHECK_IS_RECENT, (kind, url, WEEKLY_INTERVAL)).fetchone()[0]
            if recent:
                continue
            # Not counted against a schedule: the uptime checks already
            # suspend on a refused POST, and the next hour tries again.
            try:
                run_id = _create_run(api_base_url, bearer_token, url, kind=kind)
            except OSError:
                logger.exception("could not create a weekly %s check for %s", kind, url)
                continue
            logger.info(
                "scheduled weekly %s check %s for %s", kind, run_id, url, extra={"run_id": run_id}
            )
            created.append(run_id)

    return created


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    tracing_on = configure_telemetry()

    if not settings.mercury_bearer_token:
        raise RuntimeError("no MERCURY_BEARER_TOKEN: the scheduler cannot call the api")

    config = load_mercury_config(settings.mercury_config_path)

    telegram = telegram_client(settings)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        expire_due(conn, telegram)
        created = run_due_checks(
            conn,
            config.sites,
            settings.api_base_url,
            settings.mercury_bearer_token,
            telegram=telegram,
            chat_id=config.telegram_chat_id,
        )
        created += schedule_weekly_checks(
            conn, config.pages, settings.api_base_url, settings.mercury_bearer_token
        )

    logger.info("scheduler run complete, %s check(s) created", len(created))

    # The container exits within seconds of this returning. A batch span
    # processor's queue would be dropped unsent without an explicit flush.
    if tracing_on:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()


if __name__ == "__main__":  # pragma: no cover
    main()
