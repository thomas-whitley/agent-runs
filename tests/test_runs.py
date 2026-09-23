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


def test_post_runs_stores_the_type_s_registered_provider(start_server, clean_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "daily summary"}},
        headers={"Authorization": "Bearer the-real-token"},
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


def test_post_runs_requires_a_bearer_token_for_a_non_public_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "digest", "inputs": {"task": "x"}})

    assert response.status_code == 401


def test_post_runs_accepts_the_right_bearer_token_for_a_non_public_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "x"}},
        headers={"Authorization": "Bearer the-real-token"},
    )

    assert response.status_code == 201


def test_post_runs_rejects_the_wrong_bearer_token(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "x"}},
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_post_runs_never_requires_a_token_for_the_public_pytest_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "pytest", "inputs": {"task": "x"}})

    assert response.status_code == 201


def test_post_runs_refuses_a_non_public_type_when_no_token_is_configured(start_server):
    """Fail closed: an unset MERCURY_BEARER_TOKEN must not wave every caller through."""
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "digest", "inputs": {"task": "x"}})

    assert response.status_code == 401


def test_events_of_a_non_public_run_require_the_bearer_token(start_server, monkeypatch):
    """The guard list: event bodies sit behind the same token as run creation."""
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()
    created = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "x"}},
        headers={"Authorization": "Bearer the-real-token"},
    ).json()

    denied = httpx2.get(f"{base_url}/runs/{created['id']}/events")
    with httpx2.stream(
        "GET",
        f"{base_url}/runs/{created['id']}/events",
        headers={"Authorization": "Bearer the-real-token"},
    ) as allowed:
        assert allowed.status_code == 200

    assert denied.status_code == 401


def test_events_of_a_public_run_never_require_a_token(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()
    created = httpx2.post(
        f"{base_url}/runs", json={"type": "pytest", "inputs": {"task": "x"}}
    ).json()

    with httpx2.stream("GET", f"{base_url}/runs/{created['id']}/events") as response:
        assert response.status_code == 200


def _post_site_check(base_url: str, inputs: dict) -> httpx2.Response:
    return httpx2.post(
        f"{base_url}/runs",
        json={"type": "site_check", "inputs": inputs},
        headers={"Authorization": "Bearer the-real-token"},
    )


def test_a_site_check_stores_the_kind_it_was_posted_with(start_server, clean_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = _post_site_check(base_url, {"task": "https://example.com", "kind": "lighthouse"})

    assert response.status_code == 201
    with psycopg.connect(clean_db) as conn:
        kind = conn.execute(
            "SELECT check_kind FROM runs WHERE id = %s", (response.json()["id"],)
        ).fetchone()[0]
    assert kind == "lighthouse"


def test_a_site_check_posted_with_no_kind_is_an_uptime_check(start_server, clean_db, monkeypatch):
    """What the scheduler posts, and what it closes itself."""
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = _post_site_check(base_url, {"task": "https://example.com"})

    with psycopg.connect(clean_db) as conn:
        kind = conn.execute(
            "SELECT check_kind FROM runs WHERE id = %s", (response.json()["id"],)
        ).fetchone()[0]
    assert kind == "uptime"


def test_post_runs_rejects_an_unknown_check_kind(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = _post_site_check(base_url, {"task": "https://example.com", "kind": "pentest"})

    assert response.status_code == 422


def test_post_runs_rejects_a_check_kind_on_a_type_that_is_not_a_check(start_server):
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "pytest", "inputs": {"task": "x", "kind": "lighthouse"}},
    )

    assert response.status_code == 422


def test_a_run_that_is_not_a_check_has_no_check_kind(start_server, clean_db):
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "pytest", "inputs": {"task": "x"}})

    with psycopg.connect(clean_db) as conn:
        kind = conn.execute(
            "SELECT check_kind FROM runs WHERE id = %s", (response.json()["id"],)
        ).fetchone()[0]
    assert kind is None
