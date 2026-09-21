"""The agent loop: plan, retrieve, act, verify, repeat.

The run's task text is the pytest file the client posted. The agent writes
solution.py until that file passes, or until the token budget is gone.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from app.model import Model, ModelReply, extract_code
from app.retrieval import DEFAULT_LIMIT, Retriever
from app.runs import finish_run, record_step
from app.sandbox import DEFAULT_TIMEOUT_SECONDS, verify

DEFAULT_TOKEN_BUDGET = 50_000
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_MODEL_RETRY_ATTEMPTS = 4
DEFAULT_MODEL_RETRY_BACKOFF_SECONDS = 2.0

logger = logging.getLogger("agent_runs.loop")


class ClaimLost(Exception):
    """Raised when the run was taken over while this worker was still on it."""


SYSTEM_PROMPT = (
    "You write one Python module named solution.py. "
    "Reply with a single fenced python block and nothing else. "
    "The module must make the supplied pytest file pass."
)


@dataclass(frozen=True)
class LoopResult:
    status: str
    attempts: int
    tokens_used: int


def _context_block(chunks: list) -> str:
    if not chunks:
        return ""
    notes = "\n\n".join(f"{chunk.source}: {chunk.body}" for chunk in chunks)
    return f"These notes may help.\n\n{notes}\n\n"


def _first_prompt(test_code: str, context: str = "") -> str:
    return (
        f"{context}Write solution.py so that this pytest file passes."
        f"\n\n```python\n{test_code}\n```"
    )


def _retry_prompt(test_code: str, code: str, failure: str) -> str:
    return (
        f"Your last solution.py did not pass.\n\n```python\n{code}\n```\n\n"
        f"pytest said:\n\n```\n{failure}\n```\n\n"
        f"The test file is unchanged:\n\n```python\n{test_code}\n```\n\n"
        "Reply with the corrected solution.py."
    )


def _complete_with_retry(
    model: Model,
    system: str,
    prompt: str,
    attempts: int,
    backoff_seconds: float,
) -> ModelReply:
    """Model providers return transient errors. Retry before giving up on the run."""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return model.complete(system=system, prompt=prompt)
        except Exception as error:  # the provider's exception types are its own
            last_error = error
            if attempt == attempts:
                break
            wait = backoff_seconds * attempt
            logger.warning(
                "model call failed (attempt %s of %s), retrying in %ss: %s",
                attempt,
                attempts,
                wait,
                error,
            )
            if wait:
                time.sleep(wait)

    raise RuntimeError(f"the model failed {attempts} times: {last_error}") from last_error


def run_agent_loop(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    verify_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    model_retry_attempts: int = DEFAULT_MODEL_RETRY_ATTEMPTS,
    model_retry_backoff_seconds: float = DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
    on_step: Callable[[], bool] | None = None,
    retriever: Retriever | None = None,
    worker_id: str | None = None,
) -> LoopResult:
    test_code, tokens_used = conn.execute(
        "SELECT task, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()

    # A replacement worker continues where the dead one stopped. Steps are keyed
    # on (run_id, seq), so carrying on from the last seq cannot collide.
    seq, attempts = conn.execute(
        "SELECT coalesce(max(seq), 0), count(*) FILTER (WHERE kind = 'act') "
        "FROM steps WHERE run_id = %s",
        (run_id,),
    ).fetchone()

    def progress() -> None:
        """Report that a step landed, and stop if the run is no longer ours."""
        if on_step is not None and on_step() is False:
            raise ClaimLost(run_id)

    def write(step_seq: int, kind: str, output: dict, tokens: int = 0) -> None:
        written = record_step(
            conn, run_id, step_seq, kind, output=output, tokens=tokens, worker_id=worker_id
        )
        if not written and worker_id is not None:
            # Either the seq was already committed by us, or we no longer own the
            # run. Checking ownership tells the two apart.
            owner = conn.execute("SELECT claimed_by FROM runs WHERE id = %s", (run_id,)).fetchone()
            if owner is None or owner[0] != worker_id:
                raise ClaimLost(run_id)

    # Retrieval runs on a resume too, so the replacement worker prompts with the
    # same notes. Only the step record is skipped, because it already exists.
    chunks = retriever.search(conn, test_code, limit=DEFAULT_LIMIT) if retriever else []

    status = "budget_exhausted"
    error_message: str | None = None
    prompt = _first_prompt(test_code, _context_block(chunks))

    try:
        if seq == 0:
            seq += 1
            write(seq, "plan", {"text": "write solution.py, then run pytest"})
            progress()

            seq += 1
            write(
                seq,
                "retrieve",
                {"chunks": [{"id": chunk.id, "source": chunk.source} for chunk in chunks]},
            )
            progress()

        while attempts < max_attempts:
            if tokens_used >= token_budget:
                break

            # Refresh the lease before a call that may take a while, and find
            # out here rather than after it if the run is no longer ours.
            progress()

            reply = _complete_with_retry(
                model,
                SYSTEM_PROMPT,
                prompt,
                attempts=model_retry_attempts,
                backoff_seconds=model_retry_backoff_seconds,
            )
            tokens_used += reply.tokens
            attempts += 1
            code = extract_code(reply.text)

            seq += 1
            write(seq, "act", {"code": code}, tokens=reply.tokens)
            progress()

            result = verify(code, test_code, timeout_seconds=verify_timeout_seconds)

            seq += 1
            write(
                seq,
                "verify",
                {
                    "passed": result.passed,
                    "timed_out": result.timed_out,
                    "output": result.output,
                },
            )
            progress()

            if result.passed:
                status = "succeeded"
                break

            prompt = _retry_prompt(test_code, code, result.output)
        else:
            status = "failed"
    except ClaimLost:
        # Another worker owns this run now. It will finish it, and anything this
        # one writes from here would be writing over the owner.
        logger.warning("run %s was taken over, stopping without writing", run_id)
        return LoopResult(status="lost", attempts=attempts, tokens_used=tokens_used)
    except Exception as error:
        # A run must never be left claimed and running with a stream that never
        # ends. Close it, and let the worker carry on to the next one.
        logger.exception("run %s failed", run_id)
        status = "error"
        error_message = str(error)

    done_output: dict[str, object] = {"status": status, "attempts": attempts}
    if error_message is not None:
        done_output["error"] = error_message

    seq += 1
    try:
        write(seq, "done", done_output)
    except ClaimLost:
        logger.warning("run %s was taken over before it could be closed", run_id)
        return LoopResult(status="lost", attempts=attempts, tokens_used=tokens_used)

    if not finish_run(conn, run_id, status, tokens_used, worker_id=worker_id):
        logger.warning("run %s was taken over, its final status was not written", run_id)
        return LoopResult(status="lost", attempts=attempts, tokens_used=tokens_used)

    return LoopResult(status=status, attempts=attempts, tokens_used=tokens_used)
