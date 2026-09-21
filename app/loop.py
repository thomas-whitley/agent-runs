"""The agent loop: plan, retrieve, act, verify, repeat.

The run's task text is the pytest file the client posted. The agent writes
solution.py until that file passes, or until the token budget is gone.
"""

from dataclasses import dataclass

import psycopg

from app.model import Model, extract_code
from app.runs import record_step
from app.sandbox import DEFAULT_TIMEOUT_SECONDS, verify

DEFAULT_TOKEN_BUDGET = 50_000
DEFAULT_MAX_ATTEMPTS = 10

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


def run_agent_loop(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    verify_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> LoopResult:
    test_code = conn.execute("SELECT task FROM runs WHERE id = %s", (run_id,)).fetchone()[0]

    seq = 0
    tokens_used = 0
    attempts = 0

    seq += 1
    record_step(conn, run_id, seq, "plan", output={"text": "write solution.py, then run pytest"})

    seq += 1
    record_step(conn, run_id, seq, "retrieve", output={"chunks": []})

    status = "budget_exhausted"
    prompt = _first_prompt(test_code)

    while attempts < max_attempts:
        if tokens_used >= token_budget:
            break

        reply = model.complete(system=SYSTEM_PROMPT, prompt=prompt)
        tokens_used += reply.tokens
        attempts += 1
        code = extract_code(reply.text)

        seq += 1
        record_step(conn, run_id, seq, "act", output={"code": code}, tokens=reply.tokens)

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

        if result.passed:
            status = "succeeded"
            break

        prompt = _retry_prompt(test_code, code, result.output)
    else:
        status = "failed"

    seq += 1
    record_step(conn, run_id, seq, "done", output={"status": status, "attempts": attempts})

    conn.execute(
        "UPDATE runs SET status = %s, tokens_used = %s, finished_at = now() WHERE id = %s",
        (status, tokens_used, run_id),
    )

    return LoopResult(status=status, attempts=attempts, tokens_used=tokens_used)
