"""The claim endpoints the self hosted checks worker uses. A worker declares
the check kinds it can run and gets one pending site_check of those kinds,
never any other type, and never an uptime check, which the scheduler Job
runs and closes itself.
"""

import httpx2
import psycopg
import pytest

TOKEN = "the-real-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BOTH_KINDS = ["lighthouse", "broken_links"]


@pytest.fixture
def api(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", TOKEN)
    return start_server()


def insert_run(db_url: str, type_: str = "site_check", kind: str | None = "lighthouse") -> str:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO runs (task, type, check_kind) VALUES (%s, %s, %s) RETURNING id",
                ("https://example.com", type_, kind),
            ).fetchone()[0]
        )


def claim(base_url: str, kinds: list[str], worker_id: str = "checks-1") -> httpx2.Response:
    return httpx2.post(
        f"{base_url}/checks/claim",
        json={"worker_id": worker_id, "kinds": kinds},
        headers=AUTH,
    )


def test_claim_requires_the_bearer_token(api):
    response = httpx2.post(
        f"{api}/checks/claim", json={"worker_id": "checks-1", "kinds": BOTH_KINDS}
    )

    assert response.status_code == 401


def test_claim_returns_a_pending_check_with_its_lease(api, clean_db):
    run_id = insert_run(clean_db)

    response = claim(api, ["lighthouse"])

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == run_id
    assert body["url"] == "https://example.com"
    assert body["kind"] == "lighthouse"
    assert body["lease_seconds"] > 0

    with psycopg.connect(clean_db) as conn:
        row = conn.execute(
            "SELECT status, claimed_by, executor FROM runs WHERE id = %s", (run_id,)
        ).fetchone()
    assert row == ("running", "checks-1", "self_hosted")


def test_claim_answers_204_when_there_is_nothing_to_do(api):
    response = claim(api, BOTH_KINDS)

    assert response.status_code == 204


def test_claim_returns_only_the_kinds_the_worker_declared(api, clean_db):
    insert_run(clean_db, kind="broken_links")

    response = claim(api, ["lighthouse"])

    assert response.status_code == 204


def test_claim_refuses_every_type_except_a_declared_site_check(api, clean_db):
    """The token on the self hosted machine must be a strictly weaker
    credential than a session's, by construction."""
    for type_ in ("pytest", "chat", "repo_chore", "digest"):
        insert_run(clean_db, type_=type_, kind=None)
    insert_run(clean_db, kind="uptime")

    response = claim(api, BOTH_KINDS)

    assert response.status_code == 204
    with psycopg.connect(clean_db) as conn:
        claimed = conn.execute("SELECT count(*) FROM runs WHERE claimed_by IS NOT NULL").fetchone()
    assert claimed == (0,)


def test_claim_refuses_a_worker_that_declares_uptime(api, clean_db):
    insert_run(clean_db, kind="uptime")

    response = claim(api, ["uptime"])

    assert response.status_code == 422


def test_claim_refuses_a_worker_that_declares_no_kinds(api):
    response = claim(api, [])

    assert response.status_code == 422


def test_a_claimed_check_is_not_claimed_twice(api, clean_db):
    insert_run(clean_db)

    first = claim(api, ["lighthouse"], worker_id="checks-1")
    second = claim(api, ["lighthouse"], worker_id="checks-2")

    assert first.status_code == 200
    assert second.status_code == 204


def test_a_check_whose_lease_expired_is_claimed_again(api, clean_db):
    run_id = insert_run(clean_db)
    claim(api, ["lighthouse"], worker_id="checks-1")
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
            (run_id,),
        )

    response = claim(api, ["lighthouse"], worker_id="checks-2")

    assert response.status_code == 200
    assert response.json()["id"] == run_id


def heartbeat(base_url: str, run_id: str, worker_id: str = "checks-1") -> httpx2.Response:
    return httpx2.post(
        f"{base_url}/checks/{run_id}/heartbeat", json={"worker_id": worker_id}, headers=AUTH
    )


def age_heartbeat(db_url: str, run_id: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
            (run_id,),
        )


def seconds_since_heartbeat(db_url: str, run_id: str) -> float:
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT extract(epoch FROM now() - heartbeat_at) FROM runs WHERE id = %s", (run_id,)
        ).fetchone()[0]


def test_heartbeat_requires_the_bearer_token(api, clean_db):
    run_id = insert_run(clean_db)

    response = httpx2.post(f"{api}/checks/{run_id}/heartbeat", json={"worker_id": "checks-1"})

    assert response.status_code == 401


def test_heartbeat_extends_the_lease_of_the_worker_that_holds_it(api, clean_db):
    run_id = insert_run(clean_db)
    claim(api, ["lighthouse"])
    age_heartbeat(clean_db, run_id)

    response = heartbeat(api, run_id)

    assert response.status_code == 200
    assert response.json()["lease_seconds"] > 0
    assert seconds_since_heartbeat(clean_db, run_id) < 60


def test_heartbeat_from_a_worker_that_does_not_hold_the_check_is_refused(api, clean_db):
    run_id = insert_run(clean_db)
    claim(api, ["lighthouse"], worker_id="checks-1")
    age_heartbeat(clean_db, run_id)

    response = heartbeat(api, run_id, worker_id="checks-2")

    assert response.status_code == 409
    assert seconds_since_heartbeat(clean_db, run_id) > 60


def test_heartbeat_after_a_takeover_tells_the_old_worker_to_stop(api, clean_db):
    run_id = insert_run(clean_db)
    claim(api, ["lighthouse"], worker_id="checks-1")
    age_heartbeat(clean_db, run_id)
    claim(api, ["lighthouse"], worker_id="checks-2")

    response = heartbeat(api, run_id, worker_id="checks-1")

    assert response.status_code == 409


def test_heartbeat_cannot_keep_a_run_of_another_type_alive(api, clean_db):
    """Even a pytest run claimed under the same worker id is out of reach."""
    run_id = insert_run(clean_db, type_="pytest", kind=None)
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET claimed_by = 'checks-1', status = 'running', "
            "heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
            (run_id,),
        )

    response = heartbeat(api, run_id)

    assert response.status_code == 409
    assert seconds_since_heartbeat(clean_db, run_id) > 60
