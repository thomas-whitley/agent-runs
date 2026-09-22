import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from app.config import load_settings
from app.logging_setup import configure_logging
from app.migrations import apply_migrations
from app.run_list import DEFAULT_LIMIT, MAX_LIMIT, build_query, encode_cursor, serialize_run_row
from app.stream import event_stream, parse_last_event_id
from app.tasks import TASK_TYPES
from app.telemetry import configure_telemetry, start_run_trace

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class RunRequest(BaseModel):
    type: str
    inputs: dict[str, Any]

    @field_validator("type")
    @classmethod
    def type_must_be_registered(cls, value: str) -> str:
        if value not in TASK_TYPES:
            raise ValueError(f"unknown task type {value!r}")
        return value

    @field_validator("inputs")
    @classmethod
    def inputs_must_carry_a_task(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Every registered type stores its input on the same task column for
        # now; per type input shapes are future work, not this step's.
        task = value.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("inputs.task must not be blank")
        return value


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

    @app.get("/")
    def demo_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/runs", status_code=201, response_model=RunCreated)
    async def create_run(run: RunRequest, request: Request) -> RunCreated:
        task_type = TASK_TYPES[run.type]
        trace_context = start_run_trace()
        async with request.app.state.pool.connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO runs (task, type, provider, trace_context) "
                "VALUES (%s, %s, %s, %s) RETURNING id, status",
                (run.inputs["task"].strip(), run.type, task_type.provider, trace_context),
            )
            row = await cursor.fetchone()
        return RunCreated(id=row[0], status=row[1])

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

        next_cursor = encode_cursor(rows[-1][7], rows[-1][0]) if len(rows) == limit else None
        return {"runs": [serialize_run_row(row) for row in rows], "next_cursor": next_cursor}

    @app.get("/runs/{run_id}/events")
    async def stream_run_events(run_id: uuid.UUID, request: Request) -> StreamingResponse:
        pool = request.app.state.pool

        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT 1 FROM runs WHERE id = %s", (str(run_id),))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail="run not found")

        after_id = parse_last_event_id(request.headers.get("Last-Event-ID"))

        return StreamingResponse(
            event_stream(pool, run_id, after_id, request.app.state.settings.keepalive_seconds),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # After the routes exist, not from the lifespan: see configure_telemetry's
    # docstring for why the timing matters.
    configure_telemetry(app)

    return app


app = create_app()
