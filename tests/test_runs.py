import uuid

import httpx2
import psycopg


def test_post_runs_creates_a_pending_run(start_server, clean_db):
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"task": "make tests/example_test.py pass"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    run_id = uuid.UUID(body["id"])

    with psycopg.connect(clean_db) as conn:
        row = conn.execute(
            "SELECT task, status, claimed_by FROM runs WHERE id = %s", (str(run_id),)
        ).fetchone()

    assert row == ("make tests/example_test.py pass", "pending", None)


def test_post_runs_rejects_an_empty_task(start_server):
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"task": "   "})

    assert response.status_code == 422


def test_health_reports_ok(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
