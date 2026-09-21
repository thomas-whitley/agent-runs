"""Runtime configuration. Everything here is an environment variable, not code."""

import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql://agent:agent@localhost:5432/agent_runs"


@dataclass(frozen=True)
class Settings:
    database_url: str
    keepalive_seconds: float


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        keepalive_seconds=float(os.environ.get("KEEPALIVE_SECONDS", "15")),
    )
