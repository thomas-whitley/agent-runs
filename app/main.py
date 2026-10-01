import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.auth import require_bearer_token
from app.check_claims import router as check_claims_router
from app.config import load_settings
from app.logging_setup import configure_logging
from app.mercury_config import MercuryConfig, load_mercury_config
from app.migrations import apply_migrations
from app.run_list import DEFAULT_LIMIT, MAX_LIMIT, build_query, encode_cursor, serialize_run_row
from app.run_request import RunRequest
from app.stream import event_stream, parse_last_event_id
from app.tasks import TASK_TYPES
from app.telegram_webhook import router as telegram_router
from app.telemetry import configure_telemetry, start_run_trace

# web/ is built into web/dist, next to app/ both in the image and in a checkout.
DEFAULT_WEB_DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"


def _web_dist_dir() -> Path:
    return Path(os.environ.get("WEB_DIST_DIR", DEFAULT_WEB_DIST_DIR)).resolve()


class RunCreated(BaseModel):
    id: uuid.UUID
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    apply_migrations(settings.database_url)

    pool = AsyncConnectionPool(settings.database_url, open=False)
    await pool.open()

    app.state.settings = settings
    app.state.pool = pool
    # The webhook reads the chat allowlist and the site list from here. With
    # no file mounted the allowlist is empty and the bot answers no one.
    try:
        app.state.mercury = load_mercury_config(settings.mercury_config_path)
    except FileNotFoundError:
        app.state.mercury = MercuryConfig(sites=())
    try:
        yield
    finally:
        await pool.close()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="agent-runs", lifespan=lifespan)

    @app.middleware("http")
    async def name_the_replica(request: Request, call_next):
        """So a client can see which replica answered, and prove a reconnect moved."""
        response = await call_next(request)
        response.headers["X-Replica"] = request.app.state.settings.replica_id
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=201, response_model=RunCreated)
    async def create_run(run: RunRequest, request: Request) -> RunCreated:
        task_type = TASK_TYPES[run.type]
        if not task_type.public:
            require_bearer_token(request)
        if run.type == "repo_chore":
            # A chore waits for a button press in Telegram (app/chat.py), and
            # this endpoint has no way to ask, so it cannot start one.
            raise HTTPException(status_code=422, detail="a repo_chore starts from Telegram")
        trace_context = start_run_trace()
        async with request.app.state.pool.connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO runs (task, type, provider, trace_context, check_kind) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id, status",
                (
                    run.inputs["task"].strip(),
                    run.type,
                    task_type.provider,
                    trace_context,
                    run.check_kind,
                ),
            )
            row = await cursor.fetchone()
        return RunCreated(id=row[0], status=row[1])

    app.include_router(check_claims_router)
    app.include_router(telegram_router)

    @app.get("/runs")
    async def list_runs(
        request: Request,
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        cursor: str | None = None,
    ) -> dict:
        sql, params = build_query(cursor)
        async with request.app.state.pool.connection() as conn:
            result = await conn.execute(sql, [*params, limit])
            rows = await result.fetchall()

        next_cursor = (
            encode_cursor(rows[-1][7], rows[-1][0]) if rows and len(rows) == limit else None
        )
        return {"runs": [serialize_run_row(row) for row in rows], "next_cursor": next_cursor}

    @app.get("/runs/{run_id}/events")
    async def stream_run_events(
        run_id: uuid.UUID,
        request: Request,
        # A browser EventSource cannot set Last-Event-ID on a fresh
        # connection, so the runs page resumes with ?after= instead.
        after: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        pool = request.app.state.pool

        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT type FROM runs WHERE id = %s", (str(run_id),))
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        # Event bodies of a non-public run sit behind the same token as its
        # creation. With no Authorization header at all the stream still runs,
        # without bodies, which is what the runs page shows. A header that is
        # present and wrong is refused, not quietly downgraded.
        bodies = True
        if not TASK_TYPES[row[0]].public:
            if "authorization" in request.headers:
                require_bearer_token(request)
            else:
                bodies = False

        header = request.headers.get("Last-Event-ID")
        after_id = parse_last_event_id(header) if header else (after or 0)

        return StreamingResponse(
            event_stream(
                pool, run_id, after_id, request.app.state.settings.keepalive_seconds, bodies
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    web_dist = _web_dist_dir()

    # Registered last, so every API route above answers first and only an
    # unmatched GET reaches the page. A built file is served as itself;
    # anything else gets index.html, which routes in the browser.
    @app.get("/{path:path}", include_in_schema=False)
    def page(path: str):
        index = web_dist / "index.html"
        if not index.is_file():
            return PlainTextResponse("the page is not built", status_code=404)
        candidate = (web_dist / path).resolve()
        if path and candidate.is_relative_to(web_dist) and candidate.is_file():
            return FileResponse(candidate)
        # Hashed assets can be cached; the index that names them cannot, or a
        # browser keeps asking for files the last deploy deleted.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    # After the routes exist, not from the lifespan: see configure_telemetry's
    # docstring for why the timing matters.
    configure_telemetry(app)

    return app


app = create_app()
