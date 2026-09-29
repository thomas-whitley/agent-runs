"""Progress on Telegram: one message per run, edited in place as its steps
land. Only a run started from Telegram carries a chat and message id.

The text is built from step kinds and a few numbers only. Code, test output,
a model's reply and a task's input never reach the message, for the same
reason they never reach a log line.
"""

import logging
from typing import Any

import psycopg

from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.progress")

_RUN = """
SELECT type, status, telegram_chat_id, telegram_message_id, tokens_used,
       extract(epoch from (finished_at - created_at))
FROM runs WHERE id = %s
"""
_STEPS = "SELECT seq, kind, output FROM steps WHERE run_id = %s ORDER BY seq"


def _check_line(output: dict[str, Any]) -> str:
    if "error" in output and output["error"]:
        return "check failed"
    if "scores" in output:
        scores = ", ".join(f"{name} {score}" for name, score in output["scores"].items())
        lcp = f", LCP {output['lcp_ms']} ms" if output.get("lcp_ms") is not None else ""
        return f"{scores}{lcp}"
    if "pages_checked" in output:
        return f"{output['pages_checked']} pages, {output['broken_count']} broken links"
    if "status_code" in output:
        return f"HTTP {output['status_code']} in {output['latency_ms']} ms"
    return "checked"


def _step_line(seq: int, kind: str, output: dict[str, Any] | None) -> str:
    output = output or {}
    if kind == "retrieve":
        text = f"retrieved {len(output.get('chunks') or [])} notes"
    elif kind == "act":
        text = "wrote solution.py"
    elif kind == "verify":
        text = "pytest passed" if output.get("passed") else "pytest failed"
    elif kind == "check":
        text = _check_line(output)
    elif kind == "clone":
        text = (
            "picked up the branch a first worker pushed"
            if output.get("resumed")
            else f"cloned, branched from {output.get('base', 'the default branch')}"
        )
    elif kind == "read":
        count = len(output.get("files") or [])
        text = f"read {count} file{'' if count == 1 else 's'}"
    elif kind == "edit":
        count = len(output.get("files") or [])
        changed = f"changed {count} file{'' if count == 1 else 's'}" if count else "not usable"
        text = f"attempt {output.get('attempt')} {changed}"
    elif kind == "test":
        text = "tests passed" if output.get("passed") else "tests failed"
    elif kind == "push":
        text = f"pushed {output.get('branch')}"
    elif kind == "pr":
        text = f"opened {output.get('url')}"
    elif kind == "done":
        text = f"done, {output.get('status', 'finished')}"
        if output.get("reason"):
            text += f": {output['reason']}"
    else:
        text = kind
    return f"{seq} {text}"


def _duration(seconds) -> str:
    seconds = float(seconds)
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def _render(run_id: str, run: tuple, steps: list[tuple]) -> str:
    type_, status, _, _, tokens, seconds = run
    header = f"{type_} run {str(run_id)[:8]}: {status}"
    if seconds is not None:
        header += f", {tokens} tokens, {_duration(seconds)}"
    return "\n".join([header, *(_step_line(*step) for step in steps)])


def render_progress(conn: psycopg.Connection, run_id: str) -> str:
    run = conn.execute(_RUN, (run_id,)).fetchone()
    return _render(run_id, run, conn.execute(_STEPS, (run_id,)).fetchall())


def push_progress(conn: psycopg.Connection, run_id: str, telegram: TelegramClient | None) -> None:
    """Edit the run's message to show where it is. Never raises: a run does
    not fail because Telegram did."""
    if telegram is None:
        return
    run = conn.execute(_RUN, (run_id,)).fetchone()
    if run is None or run[2] is None or run[3] is None:
        return
    text = _render(run_id, run, conn.execute(_STEPS, (run_id,)).fetchall())
    try:
        telegram.edit_message_text(run[2], run[3], text)
    except TelegramError as error:
        logger.warning("progress for run %s not shown: %s", run_id, error, extra={"run_id": run_id})


async def push_progress_async(pool, run_id: str, telegram: TelegramClient | None) -> None:
    """The same, for the api's async pool, with the Telegram call off the loop."""
    from starlette.concurrency import run_in_threadpool

    if telegram is None:
        return
    async with pool.connection() as conn:
        run = await (await conn.execute(_RUN, (run_id,))).fetchone()
        if run is None or run[2] is None or run[3] is None:
            return
        steps = await (await conn.execute(_STEPS, (run_id,))).fetchall()
    text = _render(run_id, run, steps)
    try:
        await run_in_threadpool(telegram.edit_message_text, run[2], run[3], text)
    except TelegramError as error:
        logger.warning("progress for run %s not shown: %s", run_id, error, extra={"run_id": run_id})
