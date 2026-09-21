"""The worker role: claim a pending run, execute its loop, append its events."""

import logging
import time

import psycopg

from app.config import Settings, load_settings
from app.loop import LoopResult, run_agent_loop
from app.migrations import apply_migrations
from app.model import Model, StubModel
from app.runs import claim_run, record_step

logger = logging.getLogger("agent_runs.worker")

_PENDING = """
SELECT id FROM runs
WHERE status = 'pending' AND claimed_by IS NULL
ORDER BY created_at
LIMIT 5
"""

_STARTED_TODAY = """
SELECT count(*) FROM runs
WHERE claimed_by IS NOT NULL AND created_at >= date_trunc('day', now())
"""


def runs_started_today(conn: psycopg.Connection) -> int:
    return conn.execute(_STARTED_TODAY).fetchone()[0]


def claim_next_run(conn: psycopg.Connection, worker_id: str) -> str | None:
    """Claim the oldest pending run this worker can get. None means nothing to do."""
    for (run_id,) in conn.execute(_PENDING).fetchall():
        if claim_run(conn, run_id, worker_id):
            return run_id
    return None


def refuse_run(conn: psycopg.Connection, run_id: str, reason: str) -> None:
    """Close a run the worker will not execute, so its stream ends rather than hanging."""
    next_seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM events WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    record_step(conn, run_id, next_seq, "done", output={"status": "refused", "reason": reason})
    conn.execute("UPDATE runs SET status = 'refused', finished_at = now() WHERE id = %s", (run_id,))


def process_run(
    conn: psycopg.Connection, run_id: str, model: Model, settings: Settings
) -> LoopResult | None:
    """Execute one claimed run. None means the daily guard refused it."""
    if runs_started_today(conn) > settings.max_runs_per_day:
        logger.warning(
            "refusing run %s: daily limit of %s reached", run_id, settings.max_runs_per_day
        )
        refuse_run(conn, run_id, f"daily limit of {settings.max_runs_per_day} runs reached")
        return None

    return run_agent_loop(
        conn,
        run_id,
        model,
        token_budget=settings.token_budget,
        verify_timeout_seconds=settings.verify_timeout_seconds,
    )


def build_model(settings: Settings) -> Model:
    """Pick the model from what is configured, without a provider switch to maintain."""
    if settings.model == "stub":
        return StubModel(replies=[""])

    if settings.model_base_url:
        from app.model import OpenAICompatibleModel

        return OpenAICompatibleModel(
            model=settings.model,
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
        )

    if settings.anthropic_api_key:
        from app.model import AnthropicModel

        return AnthropicModel(model=settings.model, api_key=settings.anthropic_api_key)

    raise RuntimeError(
        "no model credentials: set MODEL=stub, or MODEL_BASE_URL with MODEL_API_KEY, "
        "or ANTHROPIC_API_KEY"
    )


def main() -> None:  # pragma: no cover - the process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    apply_migrations(settings.database_url)
    model = build_model(settings)

    logger.info("worker %s started on model %s", settings.worker_id, settings.model)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        while True:
            try:
                run_id = claim_next_run(conn, settings.worker_id)
                if run_id is None:
                    time.sleep(settings.poll_seconds)
                    continue
                logger.info("claimed run %s", run_id)
                process_run(conn, run_id, model, settings)
            except Exception:
                # run_agent_loop already closes a run it could not finish. This
                # catches everything outside it, so the worker outlives a blip.
                logger.exception("worker loop error, carrying on")
                time.sleep(settings.poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
