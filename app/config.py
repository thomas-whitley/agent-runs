"""Runtime configuration. Everything here is an environment variable, not code."""

import os
import socket
import uuid
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql://agent:agent@localhost:5432/agent_runs"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Settings:
    database_url: str
    keepalive_seconds: float
    model: str
    anthropic_api_key: str | None
    token_budget: int
    max_runs_per_day: int
    worker_id: str
    poll_seconds: float
    verify_timeout_seconds: float


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        keepalive_seconds=float(os.environ.get("KEEPALIVE_SECONDS", "15")),
        model=os.environ.get("MODEL", DEFAULT_MODEL),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        token_budget=int(os.environ.get("TOKEN_BUDGET", "50000")),
        max_runs_per_day=int(os.environ.get("MAX_RUNS_PER_DAY", "20")),
        worker_id=os.environ.get("WORKER_ID") or _default_worker_id(),
        poll_seconds=float(os.environ.get("POLL_SECONDS", "1")),
        verify_timeout_seconds=float(os.environ.get("VERIFY_TIMEOUT_SECONDS", "10")),
    )
