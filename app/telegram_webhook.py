"""POST /telegram, the bot's inbound half, per docs/mercury.md.

Telegram's secret token header is checked first and fails closed when no
secret is configured. Then the chat id is checked against the allowlist of
one; any other sender gets a 200 and no reply at all. /status, /runs and
/cancel are answered here with no model call. Anything else becomes a chat
run for the worker (app/chat.py).

Every update the check lets through is answered with 200, even when the
reply fails to send, because Telegram retries anything else and a retried
/cancel would run twice.
"""

import hmac
import logging
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.tasks import TASK_TYPES
from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.telegram")

router = APIRouter()

SECRET_HEADER = "x-telegram-bot-api-secret-token"
RECENT_RUNS = 5
CANCEL_USAGE = "Usage: /cancel <run id or its first 8 characters>"
# Eight hex characters is what /runs shows; anything shorter could match many.
_RUN_PREFIX = re.compile(r"^[0-9a-f-]{8,36}$")

_RECENT = """
SELECT id, type, status, tokens_used,
       extract(epoch from (finished_at - created_at)) AS seconds
FROM runs ORDER BY created_at DESC, id DESC LIMIT %s
"""

_LAST_UPTIME = """
SELECT status, created_at FROM runs
WHERE type = 'site_check' AND check_kind = 'uptime' AND task = %s
ORDER BY created_at DESC LIMIT 1
"""

_SUSPENDED = "SELECT suspended FROM schedule_state WHERE name = %s"

_TODAY = """
SELECT count(*) FILTER (WHERE claimed_by IS NOT NULL AND type <> 'site_check'),
       count(*) FILTER (WHERE status = 'running' AND finished_at IS NULL),
       count(*) FILTER (WHERE status = 'pending' AND finished_at IS NULL)
FROM runs WHERE created_at >= date_trunc('day', now())
"""

# Clearing claimed_by fences out a worker still running it: its next step
# write and heartbeat both need claimed_by to be its own id.
_CANCEL = """
UPDATE runs SET status = 'cancelled', finished_at = now(), claimed_by = NULL
WHERE id = (
    SELECT id FROM runs
    WHERE finished_at IS NULL AND id::text LIKE %s
    ORDER BY created_at DESC LIMIT 1
)
RETURNING id
"""

_NEXT_SEQ = "SELECT coalesce(max(seq), 0) + 1 FROM events WHERE run_id = %s"
_DONE_STEP = """
INSERT INTO steps (run_id, seq, kind, output, tokens, finished_at)
VALUES (%s, %s, 'done', %s, 0, now()) ON CONFLICT (run_id, seq) DO NOTHING
"""
_DONE_EVENT = """
INSERT INTO events (run_id, seq, payload) VALUES (%s, %s, %s)
ON CONFLICT (run_id, seq) DO NOTHING
"""


def _check_secret(request: Request) -> None:
    expected = request.app.state.settings.telegram_webhook_secret
    given = request.headers.get(SECRET_HEADER, "")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="missing or invalid secret token")


@router.post("/telegram", include_in_schema=False)
async def telegram_webhook(request: Request) -> dict:
    _check_secret(request)
    update = await request.json()

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")
    if chat_id is None or not isinstance(text, str):
        return {}
    if chat_id != request.app.state.mercury.telegram_chat_id:
        # No reply and no text in the log: a stranger learns nothing, and
        # their message is not ours to keep.
        logger.info("ignored an update from a chat not on the allowlist")
        return {}

    text = text.strip()
    if text.startswith("/"):
        reply = await _answer(request, text)
        if reply is not None:
            await _send(request, chat_id, reply)
        return {}

    # Free text is the worker's, which holds the model key. One placeholder
    # message now, which the worker edits with its answer, so a cold worker
    # does not leave the chat silent.
    message_id = await _send(request, chat_id, "On it.")
    async with request.app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO runs (task, type, provider, telegram_chat_id, telegram_message_id) "
                "VALUES (%s, 'chat', %s, %s, %s) RETURNING id",
                (text, TASK_TYPES["chat"].provider, chat_id, message_id),
            )
        ).fetchone()
    logger.info("chat run %s created from Telegram", row[0], extra={"run_id": str(row[0])})
    return {}


async def _answer(request: Request, text: str) -> str | None:
    command, _, argument = text.partition(" ")
    # "/runs@my_bot" is how a command arrives from a group; strip the name.
    command = command.split("@", 1)[0].lower()
    pool = request.app.state.pool
    if command == "/runs":
        return await _recent_runs(pool)
    if command == "/status":
        return await _status(pool, request.app.state.mercury.sites, request)
    if command == "/cancel":
        return await _cancel(pool, argument.strip().lower())
    return None


async def _send(request: Request, chat_id: int, text: str) -> int | None:
    """Send a message and return its id, or None when it could not be sent."""
    settings = request.app.state.settings
    if not settings.telegram_bot_token:
        logger.error("no TELEGRAM_BOT_TOKEN, so the reply was not sent")
        return None
    client = TelegramClient(settings.telegram_bot_token, settings.telegram_api_url)
    try:
        return await run_in_threadpool(client.send_message, chat_id, text)
    except TelegramError as error:
        logger.error("reply not sent: %s", error)
        return None


def _duration(seconds) -> str:
    if seconds is None:
        return "running"
    seconds = float(seconds)
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


async def _recent_runs(pool) -> str:
    async with pool.connection() as conn:
        rows = await (await conn.execute(_RECENT, (RECENT_RUNS,))).fetchall()
    if not rows:
        return "No runs yet."
    lines = [
        f"{str(run_id)[:8]} {type_} {status}, {tokens} tokens, {_duration(seconds)}"
        for run_id, type_, status, tokens, seconds in rows
    ]
    return "\n".join(lines)


def _clock(moment: datetime) -> str:
    return moment.strftime("%d %b %H:%M UTC")


async def _status(pool, sites: tuple[str, ...], request: Request) -> str:
    limit = request.app.state.settings.max_runs_per_day
    lines = []
    async with pool.connection() as conn:
        for site in sites:
            last = await (await conn.execute(_LAST_UPTIME, (site,))).fetchone()
            state = await (await conn.execute(_SUSPENDED, (f"site_uptime:{site}",))).fetchone()
            seen = f"{last[0]} at {_clock(last[1])}" if last else "not checked yet"
            suspended = ", suspended" if state and state[0] else ""
            lines.append(f"{site}: {seen}{suspended}")
        started, running, pending = await (await conn.execute(_TODAY)).fetchone()
    if not sites:
        lines.append("No sites configured.")
    lines.append(f"Runs today: {started} of {limit}. Running: {running}. Pending: {pending}.")
    return "\n".join(lines)


async def _cancel(pool, prefix: str) -> str:
    if not _RUN_PREFIX.match(prefix):
        return CANCEL_USAGE
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(_CANCEL, (f"{prefix}%",))).fetchone()
            if row is None:
                return f"No unfinished run starts with {prefix}."
            run_id = row[0]
            seq = (await (await conn.execute(_NEXT_SEQ, (run_id,))).fetchone())[0]
            done = {"status": "cancelled"}
            await conn.execute(_DONE_STEP, (run_id, seq, Jsonb(done)))
            await conn.execute(
                _DONE_EVENT, (run_id, seq, Jsonb({"kind": "done", "seq": seq, "output": done}))
            )
    logger.info("cancelled run %s from Telegram", run_id, extra={"run_id": str(run_id)})
    return f"Cancelled {prefix[:8]}."
