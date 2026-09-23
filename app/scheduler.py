"""ROLE=scheduler: a Container Apps Job that wakes on a cron trigger, checks
the configured sites, and exits. It creates each run through the API with
the bearer token, so the run exists with no client attached to watch it,
then executes and closes the run itself in the same process. A plain HTTP
check needs no browser and no separate worker, and the agent loop worker
never claims site_check runs (see _CLAIMABLE in app/worker.py).
"""

import json
import logging
import urllib.request

import psycopg
from opentelemetry import trace

from app.checks import check_site
from app.config import load_settings
from app.logging_setup import configure_logging
from app.mercury_config import load_mercury_config
from app.migrations import apply_migrations
from app.runs import finish_run, record_step
from app.schedule_state import is_suspended, record_failure, record_success
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.scheduler")


def _create_run(
    api_base_url: str, bearer_token: str, url: str, timeout_seconds: float = 10.0
) -> str:
    body = json.dumps({"type": "site_check", "inputs": {"task": url}}).encode()
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
) -> list[str]:
    """Check every configured site not currently suspended. Returns the ids
    of the runs created, so a caller (or a test) can inspect them."""
    created: list[str] = []

    for url in sites:
        schedule_name = f"site_uptime:{url}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue

        # OSError covers URLError, HTTPError (a 401 from a wrong token) and a
        # timeout while reading the response.
        try:
            run_id = _create_run(api_base_url, bearer_token, url)
        except OSError:
            logger.exception("could not create a run for %s, skipping this cycle", url)
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
            record_failure(conn, schedule_name)

    return created


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    tracing_on = configure_telemetry()

    if not settings.mercury_bearer_token:
        raise RuntimeError("no MERCURY_BEARER_TOKEN: the scheduler cannot call the api")

    config = load_mercury_config(settings.mercury_config_path)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        created = run_due_checks(
            conn, config.sites, settings.api_base_url, settings.mercury_bearer_token
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
