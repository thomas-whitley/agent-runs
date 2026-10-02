"""What the run routes do, as functions both the HTTP routes and the MCP
server (app/mcp_server.py) call, so the registry, the chore gate and the
list's fields are the same whichever way a caller comes in.

Refusals are HTTPExceptions, which the routes return as they are and the
MCP server turns into tool errors.
"""

from fastapi import HTTPException
from psycopg import connect
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.chores import ChoreRefused, find_repo, request_chore
from app.run_list import ONE_RUN, build_query, encode_cursor, serialize_run_row
from app.run_request import RunRequest
from app.tasks import TASK_TYPES
from app.telegram import TelegramClient, TelegramError
from app.telemetry import start_run_trace

_INSERT = (
    "INSERT INTO runs (task, type, provider, trace_context, check_kind, source) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, status"
)
_EVENTS = "SELECT id, payload FROM events WHERE run_id = %s AND id > %s ORDER BY id"


class RunCreated(BaseModel):
    id: str
    status: str


async def create_run(state, run: RunRequest, source: str) -> RunCreated:
    """state is the app's state, holding the pool, settings and mercury config."""
    if run.type == "repo_chore":
        return await run_in_threadpool(_request_chore, state, run, source)
    async with state.pool.connection() as conn:
        cursor = await conn.execute(
            _INSERT,
            (
                run.inputs["task"].strip(),
                run.type,
                TASK_TYPES[run.type].provider,
                start_run_trace(),
                run.check_kind,
                source,
            ),
        )
        row = await cursor.fetchone()
    return RunCreated(id=str(row[0]), status=row[1])


def _request_chore(state, run: RunRequest, source: str) -> RunCreated:
    """A chore from any caller waits for the same Approve button as one
    asked for in chat (app/chores.py). With no bot or chat to ask, it is
    refused rather than left waiting on a question nobody saw."""
    try:
        repo = find_repo(state.mercury.repos, run.inputs.get("repo"))
    except ChoreRefused as refused:
        raise HTTPException(status_code=422, detail=str(refused)) from None
    settings = state.settings
    chat_id = state.mercury.telegram_chat_id
    if not settings.telegram_bot_token or not chat_id:
        raise HTTPException(
            status_code=503, detail="a repo_chore needs Telegram to ask for approval"
        )
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_api_url)
    try:
        with connect(settings.database_url, autocommit=True) as conn:
            run_id = request_chore(conn, telegram, chat_id, repo, run.inputs["task"], source)
    except TelegramError:
        raise HTTPException(
            status_code=502, detail="the approval question could not be sent"
        ) from None
    return RunCreated(id=run_id, status="awaiting_approval")


async def list_runs(pool, limit: int, cursor: str | None) -> dict:
    sql, params = build_query(cursor)
    async with pool.connection() as conn:
        rows = await (await conn.execute(sql, [*params, limit])).fetchall()
    next_cursor = encode_cursor(rows[-1][7], rows[-1][0]) if rows and len(rows) == limit else None
    return {"runs": [serialize_run_row(row) for row in rows], "next_cursor": next_cursor}


async def get_run(pool, run_id: str) -> dict:
    async with pool.connection() as conn:
        row = await (await conn.execute(ONE_RUN, (run_id,))).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return serialize_run_row(row)


async def run_events(pool, run_id: str, after: int = 0) -> list[dict]:
    """The events written so far, with their bodies, oldest first, each with
    its event id. Not a stream: a caller asks again with after set to the
    last id it has, as the SSE route's ?after= does."""
    await get_run(pool, run_id)
    async with pool.connection() as conn:
        rows = await (await conn.execute(_EVENTS, (run_id, after))).fetchall()
    return [{"id": event_id, **payload} for event_id, payload in rows]
