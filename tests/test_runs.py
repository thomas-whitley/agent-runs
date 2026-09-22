import uuid

import httpx2
import psycopg


def test_post_runs_creates_a_pending_run(start_server, clean_db):
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "pytest", "inputs": {"task": "make tests/example_test.py pass"}},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    run_id = uuid.UUID(body["id"])

    with psycopg.connect(clean_db) as conn:
        row = conn.execute(
            "SELECT task, type, provider, status, claimed_by FROM runs WHERE id = %s",
            (str(run_id),),
        ).fetchone()

    assert row == ("make tests/example_test.py pass", "pytest", "gemini", "pending", None)


def test_a_run_created_with_tracing_off_stores_no_trace_context(
    start_server, clean_db, monkeypatch
):
    """NULL, not an empty string, so the column keeps one meaning."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "pytest", "inputs": {"task": "make tests/example_test.py pass"}},
    )
    run_id = response.json()["id"]

    with psycopg.connect(clean_db) as conn:
        trace_context = conn.execute(
            "SELECT trace_context FROM runs WHERE id = %s", (run_id,)
        ).fetchone()[0]

    assert trace_context is None


def test_post_runs_rejects_an_empty_task(start_server):
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "pytest", "inputs": {"task": "   "}})

    assert response.status_code == 422


def test_post_runs_rejects_an_unregistered_type(start_server):
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "not_a_type", "inputs": {"task": "x"}})

    assert response.status_code == 422


def test_post_runs_stores_the_type_s_registered_provider(start_server, clean_db):
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs", json={"type": "digest", "inputs": {"task": "daily summary"}}
    )

    run_id = response.json()["id"]
    with psycopg.connect(clean_db) as conn:
        provider = conn.execute("SELECT provider FROM runs WHERE id = %s", (run_id,)).fetchone()[0]

    assert provider == "gemini"


def test_health_reports_ok(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_response_names_the_replica_that_served_it(start_server, monkeypatch):
    """Without this there is no way to show a reconnect landed on the other replica."""
    monkeypatch.setenv("REPLICA_ID", "replica-one")
    base_url = start_server()

    response = httpx2.get(f"{base_url}/health")

    assert response.headers["X-Replica"] == "replica-one"


def test_the_replica_id_defaults_to_the_hostname(start_server, monkeypatch):
    import socket

    monkeypatch.delenv("REPLICA_ID", raising=False)
    base_url = start_server()

    response = httpx2.get(f"{base_url}/health")

    assert response.headers["X-Replica"] == socket.gethostname()
