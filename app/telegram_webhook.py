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
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app import approvals
from app.runs import CANCEL_RUN, CANCELLED, DONE_EVENT, DONE_STEP, NEXT_EVENT_SEQ
from app.schedule_state import RESUME
from app.tasks import TASK_TYPES
from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.telegram")

router = APIRouter()

SECRET_HEADER = "x-telegram-bot-api-secret-token"
RECENT_RUNS = 5
CANCEL_USAGE = "Usage: /cancel <run id or its first 8 characters>"
# Eight hex characters is what /runs shows; anything shorter could match many.
_RUN_PREFIX = re.compile(r"^[0-9a-f-]{8,36}$")
_BUTTON = re.compile(r"^approval:(\d+):(yes|no)$")

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

_FIND_UNFINISHED = """
SELECT id FROM runs
WHERE finished_at IS NULL AND id::text LIKE %s
ORDER BY created_at DESC LIMIT 1
"""

_SUSPENDED_NAMES = "SELECT name FROM schedule_state WHERE suspended ORDER BY name"
RESUME_USAGE = "Usage: /resume <site>"
UPTIME_PREFIX = "site_uptime:"


def _check_secret(request: Request) -> None:
    expected = request.app.state.settings.telegram_webhook_secret
    given = request.headers.get(SECRET_HEADER, "")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="missing or invalid secret token")


@router.post("/telegram", include_in_schema=False)
async def telegram_webhook(request: Request) -> dict:
    _check_secret(request)
    update = await request.json()

    if "callback_query" in update:
        await _on_button(request, update["callback_query"])
        return {}

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
            _log_wait(message, text.split(" ", 1)[0].split("@", 1)[0].lower())
        return {}

    # Free text is the worker's, which holds the model key. One placeholder
    # message now, which the worker edits with its answer, so a cold worker
    # does not leave the chat silent.
    message_id = await _send(request, chat_id, "On it.")
    _log_wait(message, "free text")
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


def _log_wait(message: dict, what: str) -> None:
    """How long after the message was sent it was answered. Telegram stamps
    each message with the second it left the phone, so on the first message
    after idle this is the cold start as the sender felt it."""
    sent = message.get("date")
    if not isinstance(sent, int):
        return
    logger.info("answered %s %.1f s after it was sent", what, time.time() - sent)


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
    if command == "/resume":
        return await _resume(pool, argument.strip())
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
            row = await (await conn.execute(_FIND_UNFINISHED, (f"{prefix}%",))).fetchone()
            if row is None or not await _close_cancelled(conn, row[0]):
                return f"No unfinished run starts with {prefix}."
    logger.info("cancelled run %s from Telegram", row[0], extra={"run_id": str(row[0])})
    return f"Cancelled {prefix[:8]}."


async def _close_cancelled(conn, run_id) -> bool:
    """The async twin of app.runs.cancel_run, inside the caller's transaction."""
    if (await conn.execute(CANCEL_RUN, (run_id,))).rowcount == 0:
        return False
    seq = (await (await conn.execute(NEXT_EVENT_SEQ, (run_id,))).fetchone())[0]
    await conn.execute(DONE_STEP, (run_id, seq, Jsonb(CANCELLED)))
    await conn.execute(
        DONE_EVENT, (run_id, seq, Jsonb({"kind": "done", "seq": seq, "output": CANCELLED}))
    )
    return True


async def _resume(pool, site: str) -> str:
    async with pool.connection() as conn:
        if not site:
            names = [row[0] for row in await (await conn.execute(_SUSPENDED_NAMES)).fetchall()]
            listed = "\n".join(name.removeprefix(UPTIME_PREFIX) for name in names)
            return f"{RESUME_USAGE}\nSuspended:\n{listed}" if names else "Nothing is suspended."
        name = site if site.startswith(UPTIME_PREFIX) else f"{UPTIME_PREFIX}{site}"
        if (await conn.execute(RESUME, (name,))).rowcount == 0:
            return f"No schedule named {site}."
    logger.info("resumed %s from Telegram", name)
    return f"Resumed {site}."


async def _on_button(request: Request, callback: dict) -> None:
    """A press on an approval's button. Only the allowed chat's presses count,
    and a question already answered or past its expiry changes nothing more."""
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    if chat_id != request.app.state.mercury.telegram_chat_id:
        logger.info("ignored a button press from a chat not on the allowlist")
        return
    match = _BUTTON.match(callback.get("data") or "")
    if match is None:
        return
    approval_id, choice = int(match[1]), match[2]

    async with request.app.state.pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(approvals.LOCK, (approval_id,))).fetchone()
            if row is None or row[3] != chat_id:
                return
            action, run_id, schedule_name, _, message_id, text, answer, expired = row
            if answer is not None:
                note = None
                notice = f"Already {answer}."
            elif expired:
                note = approvals.EXPIRED_NOTE
                await conn.execute(approvals.ANSWER, ("expired", approval_id))
                if action == "start_run":
                    await _close_cancelled(conn, run_id)
            elif choice == "yes":
                note = approvals.APPROVED_NOTE[action]
                await conn.execute(approvals.ANSWER, ("approved", approval_id))
                if action == "start_run":
                    await conn.execute(approvals.RELEASE_RUN, (run_id,))
                else:
                    await conn.execute(RESUME, (schedule_name,))
            else:
                note = approvals.DECLINED_NOTE[action]
                await conn.execute(approvals.ANSWER, ("declined", approval_id))
                if action == "start_run":
                    await _close_cancelled(conn, run_id)
    logger.info("approval %s answered %s", approval_id, note or "again")

    client = _client(request)
    if client is None:
        return
    try:
        if note is not None and message_id is not None:
            await run_in_threadpool(
                client.edit_message_text, chat_id, message_id, f"{text}\n\n{note}"
            )
        await run_in_threadpool(
            client.answer_callback_query, callback.get("id", ""), note or notice
        )
    except TelegramError as error:
        logger.error("could not show the answer in the chat: %s", error)


def _client(request: Request) -> TelegramClient | None:
    settings = request.app.state.settings
    if not settings.telegram_bot_token:
        return None
    return TelegramClient(settings.telegram_bot_token, settings.telegram_api_url)
