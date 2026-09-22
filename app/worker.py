"""The worker role: claim a pending run, execute its loop, append its events."""

import logging
import time

import psycopg
from opentelemetry import trace

from app.config import Settings, load_settings
from app.corpus import load_corpus
from app.logging_setup import configure_logging
from app.loop import LoopResult, run_agent_loop
from app.migrations import apply_migrations
from app.model import Model, StubModel
from app.retrieval import Retriever, build_retriever, index_corpus
from app.runs import claim_run, heartbeat, record_step
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.worker")

# Unclaimed runs, and runs whose worker stopped reporting for longer than the
# lease. The second case is a worker that was killed outright.
_CLAIMABLE = """
SELECT id FROM runs
WHERE finished_at IS NULL
  AND (
        (status = 'pending' AND claimed_by IS NULL)
        OR (status = 'running' AND heartbeat_at < now() - make_interval(secs => %s))
      )
ORDER BY created_at
LIMIT 5
"""

_STARTED_TODAY = """
SELECT count(*) FROM runs
WHERE claimed_by IS NOT NULL AND created_at >= date_trunc('day', now())
"""


def runs_started_today(conn: psycopg.Connection) -> int:
    return conn.execute(_STARTED_TODAY).fetchone()[0]


def claim_next_run(
    conn: psycopg.Connection, worker_id: str, lease_seconds: float = 60.0
) -> str | None:
    """Claim the oldest claimable run. None means there is nothing to do."""
    for (run_id,) in conn.execute(_CLAIMABLE, (lease_seconds,)).fetchall():
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


def process_run(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    settings: Settings,
    retriever: Retriever | None = None,
    tracer: trace.Tracer | None = None,
) -> LoopResult | None:
    """Execute one claimed run. None means the daily guard refused it."""
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
        provider=settings.model,
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
            timeout_seconds=settings.model_timeout_seconds,
        )

    if settings.anthropic_api_key:
        from app.model import AnthropicModel

        return AnthropicModel(
            model=settings.model,
            api_key=settings.anthropic_api_key,
            timeout_seconds=settings.model_timeout_seconds,
        )

    raise RuntimeError(
        "no model credentials: set MODEL=stub, or MODEL_BASE_URL with MODEL_API_KEY, "
        "or ANTHROPIC_API_KEY"
    )


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    # No FastAPI app here to instrument; this turns on the same global tracer
    # provider the loop's step spans pick up ambiently.
    configure_telemetry()
    model = build_model(settings)
    retriever = build_retriever(settings)

    logger.info(
        "worker %s started on model %s, retrieval by %s",
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
                run_id = claim_next_run(conn, settings.worker_id, settings.lease_seconds)
                if run_id is None:
                    time.sleep(settings.poll_seconds)
                    continue
                logger.info(
                    "claimed run %s",
                    run_id,
                    extra={"run_id": run_id, "worker_id": settings.worker_id},
                )
                process_run(conn, run_id, model, settings, retriever)
            except Exception:
                # run_agent_loop already closes a run it could not finish. This
                # catches everything outside it, so the worker outlives a blip.
                logger.exception("worker loop error, carrying on")
                time.sleep(settings.poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    main()
