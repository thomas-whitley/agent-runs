"""Claiming pending runs, and the guard that stops a public URL burning the key."""

import pytest

from app.config import Settings
from app.model import OpenAICompatibleModel, StubModel
from app.worker import build_model, claim_next_run, process_run, runs_started_today

PASSING_TEST = """
from solution import add

def test_add():
    assert add(2, 3) == 5
"""

CORRECT = "```python\ndef add(a, b):\n    return a + b\n```"


def settings_with(max_runs_per_day: int = 20, **overrides) -> Settings:
    defaults = dict(
        database_url="unused",
        keepalive_seconds=15.0,
        model="stub",
        anthropic_api_key=None,
        model_base_url=None,
        model_api_key=None,
        token_budget=50_000,
        max_runs_per_day=max_runs_per_day,
        worker_id="worker-test",
        poll_seconds=0.05,
        verify_timeout_seconds=10.0,
    )
    return Settings(**{**defaults, **overrides})


def new_run(conn, task: str = PASSING_TEST) -> str:
    return conn.execute("INSERT INTO runs (task) VALUES (%s) RETURNING id", (task,)).fetchone()[0]


def test_claim_next_run_takes_a_pending_run_once(migrated_db):
    run_id = new_run(migrated_db)

    assert claim_next_run(migrated_db, "worker-a") == run_id
    assert claim_next_run(migrated_db, "worker-b") is None


def test_claim_next_run_returns_none_when_there_is_nothing_to_do(migrated_db):
    assert claim_next_run(migrated_db, "worker-a") is None


def test_a_claimed_run_is_processed_to_completion(migrated_db):
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-a")

    result = process_run(migrated_db, run_id, StubModel(replies=[CORRECT]), settings_with())

    assert result is not None
    assert result.status == "succeeded"


def test_runs_started_today_ignores_earlier_days(migrated_db):
    migrated_db.execute(
        "INSERT INTO runs (task, claimed_by, created_at) "
        "VALUES (%s, %s, now() - interval '2 days')",
        (PASSING_TEST, "worker-old"),
    )
    migrated_db.execute(
        "INSERT INTO runs (task, claimed_by) VALUES (%s, %s)", (PASSING_TEST, "worker-a")
    )

    assert runs_started_today(migrated_db) == 1


def test_the_daily_limit_refuses_the_run_and_closes_its_stream(migrated_db):
    settings = settings_with(max_runs_per_day=2)
    model = StubModel(replies=[CORRECT])

    statuses = []
    for _ in range(3):
        run_id = new_run(migrated_db)
        claim_next_run(migrated_db, "worker-a")
        process_run(migrated_db, run_id, model, settings)
        statuses.append(
            migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
        )

    assert statuses == ["succeeded", "succeeded", "refused"]

    refused_id = migrated_db.execute("SELECT id FROM runs WHERE status = 'refused'").fetchone()[0]
    payload = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s ORDER BY seq DESC LIMIT 1", (refused_id,)
    ).fetchone()[0]

    assert payload["kind"] == "done", "a refused run must still close its stream"
    assert payload["output"]["status"] == "refused"


def test_build_model_returns_the_stub_when_the_model_is_stub():
    assert isinstance(build_model(settings_with()), StubModel)


def test_build_model_uses_an_openai_compatible_endpoint_when_a_base_url_is_set():
    settings = settings_with(
        model="gemini-3.8-flash",
        model_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model_api_key="not-a-real-key",
    )

    assert isinstance(build_model(settings), OpenAICompatibleModel)


def test_build_model_refuses_when_no_key_is_configured():
    settings = settings_with(model="claude-haiku-4-5-20251001")

    with pytest.raises(RuntimeError, match="no model credentials"):
        build_model(settings)
