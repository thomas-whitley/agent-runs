"""Runtime configuration. Everything here is an environment variable, not code."""

import os
import socket
import uuid
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql://agent:agent@localhost:5432/agent_runs"
# Settings.model no longer names which model runs; PROVIDERS does that per
# provider. This default only has to be something other than "stub".
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_EMBEDDING_MODEL = "voyage-3"


@dataclass(frozen=True)
class ProviderConfig:
    """One entry of PROVIDERS. api_key_env names the secret, never holds it."""

    kind: str  # "openai_compatible" or "anthropic"
    base_url: str | None
    api_key_env: str
    model: str


# Chosen per task type by the registry in app/tasks.py, not by MODEL. Both
# entries reuse the env var names the deploy already has: MODEL_API_KEY for
# Gemini's free tier key, ANTHROPIC_API_KEY for the Haiku 4.5 task types.
PROVIDERS: dict[str, ProviderConfig] = {
    "gemini": ProviderConfig(
        kind="openai_compatible",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="MODEL_API_KEY",
        model="gemini-3.5-flash-lite",
    ),
    "haiku": ProviderConfig(
        kind="anthropic",
        base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        model="claude-haiku-4-5-20251001",
    ),
}


@dataclass(frozen=True)
class Settings:
    database_url: str
    keepalive_seconds: float
    model: str
    token_budget: int
    max_runs_per_day: int
    worker_id: str
    poll_seconds: float
    verify_timeout_seconds: float
    lease_seconds: float
    model_timeout_seconds: float
    replica_id: str
    voyage_api_key: str | None
    embedding_model: str


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        keepalive_seconds=float(os.environ.get("KEEPALIVE_SECONDS", "15")),
        model=os.environ.get("MODEL", DEFAULT_MODEL),
        token_budget=int(os.environ.get("TOKEN_BUDGET", "50000")),
        max_runs_per_day=int(os.environ.get("MAX_RUNS_PER_DAY", "20")),
        worker_id=os.environ.get("WORKER_ID") or _default_worker_id(),
        poll_seconds=float(os.environ.get("POLL_SECONDS", "1")),
        verify_timeout_seconds=float(os.environ.get("VERIFY_TIMEOUT_SECONDS", "10")),
        lease_seconds=float(os.environ.get("LEASE_SECONDS", "60")),
        model_timeout_seconds=float(os.environ.get("MODEL_TIMEOUT_SECONDS", "25")),
        replica_id=os.environ.get("REPLICA_ID") or socket.gethostname(),
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        embedding_model=os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
    )
