"""Claiming pending runs, and the guard that stops a public URL burning the key."""

import pytest

from app.config import Settings
from app.model import AnthropicModel, OpenAICompatibleModel, StubModel
from app.worker import build_model, claim_next_run, process_run, runs_started_today

PASSING_TEST = """
from solution import add

def test_add():
    assert add(2, 3) == 5
"""

CORRECT = "```python\ndef add(a, b):\n    return a + b\n```"


def stub_model_builder(reply: str = CORRECT):
    """A model_builder that ignores the provider and always returns the same stub."""
    return lambda settings, provider_name: StubModel(replies=[reply])


def settings_with(max_runs_per_day: int = 20, **overrides) -> Settings:
    defaults = dict(
        database_url="unused",
        keepalive_seconds=15.0,
        model="stub",
        token_budget=50_000,
        max_runs_per_day=max_runs_per_day,
        worker_id="worker-test",
        poll_seconds=0.05,
        verify_timeout_seconds=10.0,
        lease_seconds=60.0,
        model_timeout_seconds=25.0,
        replica_id="replica-test",
        voyage_api_key=None,
        mercury_bearer_token=None,
        api_base_url="http://localhost:8000",
        mercury_config_path="/config/mercury.yaml",
        embedding_model="voyage-3",
    )
    return Settings(**{**defaults, **overrides})


def new_run(conn, task: str = PASSING_TEST) -> str:
    return conn.execute("INSERT INTO runs (task) VALUES (%s) RETURNING id", (task,)).fetchone()[0]


def test_claim_next_run_takes_a_pending_run_once(migrated_db):
    run_id = new_run(migrated_db)

    assert claim_next_run(migrated_db, "worker-a") == run_id
    assert claim_next_run(migrated_db, "worker-b") is None


def test_claim_next_run_leaves_site_check_runs_to_the_scheduler(migrated_db):
    """The scheduler creates and closes site_check runs itself. Six older ones
    fill more than the claim query's LIMIT 5, so filtering after the query
    would still miss the pytest run behind them."""
    for _ in range(6):
        migrated_db.execute(
            "INSERT INTO runs (task, type, created_at) "
            "VALUES ('https://example.com', 'site_check', now() - interval '1 minute')"
        )
    pytest_run = new_run(migrated_db)

    assert claim_next_run(migrated_db, "worker-a") == pytest_run
    assert claim_next_run(migrated_db, "worker-b") is None


def test_claim_next_run_returns_none_when_there_is_nothing_to_do(migrated_db):
    assert claim_next_run(migrated_db, "worker-a") is None


def test_a_claimed_run_is_processed_to_completion(migrated_db):
    run_id = new_run(migrated_db)
    # The same worker id the settings carry, because the loop fences its writes
    # on the worker that owns the run.
    claim_next_run(migrated_db, "worker-test")

    result = process_run(migrated_db, run_id, settings_with(), model_builder=stub_model_builder())

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


def test_runs_started_today_ignores_site_checks(migrated_db):
    """A checks worker claims site_check runs too. They make no model call,
    so counting them would let weekly checks use up the public daily limit."""
    migrated_db.execute(
        "INSERT INTO runs (task, type, check_kind, claimed_by) "
        "VALUES ('https://example.com', 'site_check', 'lighthouse', 'checks-1')"
    )
    migrated_db.execute(
        "INSERT INTO runs (task, claimed_by) VALUES (%s, %s)", (PASSING_TEST, "worker-a")
    )

    assert runs_started_today(migrated_db) == 1


def test_the_daily_limit_refuses_the_run_and_closes_its_stream(migrated_db):
    settings = settings_with(max_runs_per_day=2)

    statuses = []
    for _ in range(3):
        run_id = new_run(migrated_db)
        claim_next_run(migrated_db, "worker-test")
        process_run(migrated_db, run_id, settings, model_builder=stub_model_builder())
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


def test_process_run_refuses_a_type_with_no_executor_yet(migrated_db):
    """chat has no loop of its own until step 3. Its stream must still end."""
    run_id = migrated_db.execute(
        "INSERT INTO runs (task, type) VALUES (%s, %s) RETURNING id", (PASSING_TEST, "chat")
    ).fetchone()[0]
    claim_next_run(migrated_db, "worker-test")

    result = process_run(migrated_db, run_id, settings_with(), model_builder=stub_model_builder())

    assert result is None
    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
    assert status == "refused"


def test_process_run_refuses_a_run_when_its_provider_has_no_credentials(migrated_db):
    """Before this refusal, a missing key left the run claimed and running forever."""
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-test")

    def failing_builder(settings, provider_name):
        raise RuntimeError(f"no model credentials: set {provider_name}_KEY")

    result = process_run(migrated_db, run_id, settings_with(), model_builder=failing_builder)

    assert result is None
    status, payload = migrated_db.execute(
        "SELECT r.status, e.payload FROM runs r "
        "JOIN events e ON e.run_id = r.id "
        "WHERE r.id = %s ORDER BY e.seq DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    assert status == "refused"
    assert payload["kind"] == "done", "a refused run must still close its stream"
    assert "no model credentials" in payload["output"]["reason"]


def test_build_model_returns_the_stub_when_the_model_is_stub():
    assert isinstance(build_model(settings_with(), "gemini"), StubModel)


def test_build_model_uses_the_openai_compatible_client_for_gemini(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key")
    settings = settings_with(model="gemini-3.8-flash")

    assert isinstance(build_model(settings, "gemini"), OpenAICompatibleModel)


def test_build_model_uses_the_anthropic_client_for_haiku(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    settings = settings_with(model="claude-haiku-4-5-20251001")

    assert isinstance(build_model(settings, "haiku"), AnthropicModel)


def test_build_model_refuses_when_no_key_is_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = settings_with(model="claude-haiku-4-5-20251001")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_model(settings, "haiku")


def test_build_model_refuses_an_unregistered_provider():
    settings = settings_with(model="claude-haiku-4-5-20251001")

    with pytest.raises(RuntimeError, match="not_a_provider"):
        build_model(settings, "not_a_provider")


def test_claim_next_run_takes_over_a_run_whose_worker_stopped_reporting(migrated_db):
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-a", lease_seconds=60)
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )

    assert claim_next_run(migrated_db, "worker-b", lease_seconds=60) == run_id


def test_claim_next_run_leaves_a_live_claim_alone(migrated_db):
    new_run(migrated_db)
    claim_next_run(migrated_db, "worker-a", lease_seconds=60)

    assert claim_next_run(migrated_db, "worker-b", lease_seconds=60) is None


def test_claim_next_run_ignores_a_finished_run(migrated_db):
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-a", lease_seconds=60)
    migrated_db.execute(
        "UPDATE runs SET status = 'succeeded', finished_at = now(), "
        "heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
        (run_id,),
    )

    assert claim_next_run(migrated_db, "worker-b", lease_seconds=60) is None


def test_processing_a_run_refreshes_its_heartbeat(migrated_db):
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-test", lease_seconds=60)
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )

    process_run(migrated_db, run_id, settings_with(), model_builder=stub_model_builder())

    age = migrated_db.execute(
        "SELECT extract(epoch from (now() - heartbeat_at)) FROM runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert age < 60, "the worker must report progress while it runs"


def test_process_run_gives_the_loop_its_retriever(migrated_db):
    from app.retrieval import CorpusChunk, TextRetriever, index_corpus

    index_corpus(
        migrated_db,
        [CorpusChunk(source="notes", ord=0, body="Use solution to add two integers together.")],
    )
    run_id = new_run(migrated_db)
    claim_next_run(migrated_db, "worker-test", lease_seconds=60)

    process_run(
        migrated_db,
        run_id,
        settings_with(),
        retriever=TextRetriever(),
        model_builder=stub_model_builder(),
    )

    output = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'retrieve'", (run_id,)
    ).fetchone()[0]
    assert output["chunks"], "the retriever never reached the loop"


def test_a_text_retriever_needs_no_embedder():
    from app.retrieval import TextRetriever

    assert TextRetriever().embedder is None


def test_a_vector_retriever_exposes_its_embedder():
    from app.retrieval import VectorRetriever, VoyageEmbedder

    embedder = VoyageEmbedder("not-a-real-key")

    assert VectorRetriever(embedder).embedder is embedder


# The cloud fallback, per docs/mercury.md: a self hosted check nobody claims
# within the window goes to the cloud path, and a lapsed lease starts the
# window again. A small window keeps the ages in these tests readable.
WINDOW = 600.0


def new_check(conn, kind: str = "lighthouse", age: str = "0 seconds") -> str:
    return conn.execute(
        "INSERT INTO runs (task, type, check_kind, created_at) "
        "VALUES ('https://example.com', 'site_check', %s, now() - %s::interval) RETURNING id",
        (kind, age),
    ).fetchone()[0]


def claim_for_cloud(conn, worker_id: str = "worker-a") -> str | None:
    return claim_next_run(conn, worker_id, lease_seconds=60, check_claim_window_seconds=WINDOW)


def test_claim_next_run_takes_a_check_nobody_claimed_within_the_window(migrated_db):
    check = new_check(migrated_db, age="11 minutes")

    assert claim_for_cloud(migrated_db) == check


def test_claim_next_run_leaves_a_check_still_inside_the_window(migrated_db):
    new_check(migrated_db, age="9 minutes")

    assert claim_for_cloud(migrated_db) is None


@pytest.mark.parametrize("kind", ["broken_links", "lighthouse"])
def test_claim_next_run_takes_both_self_hosted_kinds(migrated_db, kind):
    check = new_check(migrated_db, kind=kind, age="11 minutes")

    assert claim_for_cloud(migrated_db) == check


def test_claim_next_run_never_takes_an_uptime_check_however_old(migrated_db):
    """The scheduler opens and closes uptime checks itself."""
    new_check(migrated_db, kind="uptime", age="1 day")

    assert claim_for_cloud(migrated_db) is None


def test_a_lapsed_lease_starts_the_window_again(migrated_db):
    """The self hosted worker stopped reporting 5 minutes ago. Its 60 second
    lease lapsed 4 minutes ago, which is inside the 10 minute window, so the
    check waits for a self hosted worker to take it again."""
    check = new_check(migrated_db, age="1 hour")
    migrated_db.execute(
        "UPDATE runs SET claimed_by = 'laptop', status = 'running', executor = 'self_hosted', "
        "heartbeat_at = now() - interval '5 minutes' WHERE id = %s",
        (check,),
    )

    assert claim_for_cloud(migrated_db) is None


def test_a_check_whose_lease_lapsed_a_window_ago_goes_to_the_cloud(migrated_db):
    check = new_check(migrated_db, age="1 hour")
    migrated_db.execute(
        "UPDATE runs SET claimed_by = 'laptop', status = 'running', executor = 'self_hosted', "
        "heartbeat_at = now() - interval '12 minutes' WHERE id = %s",
        (check,),
    )

    assert claim_for_cloud(migrated_db) == check


def test_a_check_taken_over_from_the_laptop_shows_the_cloud_executor(migrated_db):
    """GET /runs shows which executor ran a check, and that listing is the
    proof for the fallback claim, so a takeover must not keep self_hosted."""
    check = new_check(migrated_db, age="1 hour")
    migrated_db.execute(
        "UPDATE runs SET claimed_by = 'laptop', status = 'running', executor = 'self_hosted', "
        "heartbeat_at = now() - interval '12 minutes' WHERE id = %s",
        (check,),
    )
    claim_for_cloud(migrated_db)

    executor = migrated_db.execute("SELECT executor FROM runs WHERE id = %s", (check,)).fetchone()
    assert executor == ("cloud",)


def test_a_pytest_run_claimed_by_the_worker_has_no_executor(migrated_db):
    """executor is only written for checks."""
    run_id = new_run(migrated_db)
    claim_for_cloud(migrated_db)

    executor = migrated_db.execute("SELECT executor FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert executor == (None,)
