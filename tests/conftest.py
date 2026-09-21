import os
from urllib.parse import urlsplit

import psycopg
import pytest

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}

DEFAULT_TEST_DATABASE_URL = "postgresql://agent:agent@localhost:5432/agent_runs"


def _test_database_url() -> str:
    """The database the tests are allowed to destroy.

    Deliberately not DATABASE_URL. That variable points at the real Supabase
    project in local .env files, and these fixtures drop the public schema.
    """
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    host = urlsplit(url).hostname or ""
    if host not in LOCAL_HOSTS and not os.environ.get("ALLOW_REMOTE_TEST_DB"):
        raise pytest.UsageError(
            f"TEST_DATABASE_URL points at host '{host}', which is not local. "
            "These tests drop the public schema. Set ALLOW_REMOTE_TEST_DB=1 only "
            "if you are certain that database is disposable."
        )
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    try:
        with psycopg.connect(url, connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        raise pytest.UsageError(
            f"Cannot reach the test database at {urlsplit(url).hostname}. "
            "Start it with: docker compose up -d db --wait\n"
            f"psycopg said: {exc}"
        ) from exc
    return url


@pytest.fixture
def clean_db(database_url: str) -> str:
    """An empty public schema, for one test."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return database_url


@pytest.fixture
def start_server(clean_db: str, monkeypatch: pytest.MonkeyPatch):
    """Start the app under a real uvicorn on a real socket.

    Starlette's TestClient buffers a streaming response instead of delivering it
    chunk by chunk, so it cannot show a client dropping part way through a live
    stream. These tests need a real connection to drop.
    """
    import threading
    import time

    import uvicorn

    from app.main import create_app

    running: list[tuple] = []

    def _start(keepalive_seconds: float = 15.0) -> str:
        monkeypatch.setenv("DATABASE_URL", clean_db)
        monkeypatch.setenv("KEEPALIVE_SECONDS", str(keepalive_seconds))

        config = uvicorn.Config(
            create_app(), host="127.0.0.1", port=0, log_level="warning", access_log=False
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start within 20 seconds")
            time.sleep(0.02)

        port = server.servers[0].sockets[0].getsockname()[1]
        running.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield _start

    for server, thread in running:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def migrated_db(clean_db: str):
    """A connection to a migrated, empty database, for code that does not go through the api."""
    from app.migrations import apply_migrations

    apply_migrations(clean_db)
    with psycopg.connect(clean_db, autocommit=True) as conn:
        yield conn
