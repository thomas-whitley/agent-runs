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
from app.runs import record_step
from app.sandbox import DEFAULT_TIMEOUT_SECONDS, verify

DEFAULT_TOKEN_BUDGET = 50_000
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_MODEL_RETRY_ATTEMPTS = 4
DEFAULT_MODEL_RETRY_BACKOFF_SECONDS = 2.0

logger = logging.getLogger("agent_runs.loop")

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


def _first_prompt(test_code: str) -> str:
    return f"Write solution.py so that this pytest file passes.\n\n```python\n{test_code}\n```"


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
    on_step: Callable[[], None] | None = None,
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
        if on_step is not None:
            on_step()

    if seq == 0:
        seq += 1
        record_step(
            conn, run_id, seq, "plan", output={"text": "write solution.py, then run pytest"}
        )
        progress()

        seq += 1
        record_step(conn, run_id, seq, "retrieve", output={"chunks": []})
        progress()

    status = "budget_exhausted"
    error_message: str | None = None
    prompt = _first_prompt(test_code)

    try:
        while attempts < max_attempts:
            if tokens_used >= token_budget:
                break

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
            record_step(conn, run_id, seq, "act", output={"code": code}, tokens=reply.tokens)
            progress()

            result = verify(code, test_code, timeout_seconds=verify_timeout_seconds)

            seq += 1
            record_step(
                conn,
                run_id,
                seq,
                "verify",
                output={
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
    record_step(conn, run_id, seq, "done", output=done_output)

    conn.execute(
        "UPDATE runs SET status = %s, tokens_used = %s, finished_at = now() WHERE id = %s",
        (status, tokens_used, run_id),
    )

    return LoopResult(status=status, attempts=attempts, tokens_used=tokens_used)
