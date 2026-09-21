import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from app.config import load_settings
from app.migrations import apply_migrations
from app.stream import event_stream, parse_last_event_id


class RunRequest(BaseModel):
    task: str

    @field_validator("task")
    @classmethod
    def task_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task must not be blank")
        return stripped


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
    app = FastAPI(title="agent-runs", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=201, response_model=RunCreated)
    async def create_run(run: RunRequest, request: Request) -> RunCreated:
        async with request.app.state.pool.connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO runs (task) VALUES (%s) RETURNING id, status", (run.task,)
            )
            row = await cursor.fetchone()
        return RunCreated(id=row[0], status=row[1])

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

    return app


app = create_app()
