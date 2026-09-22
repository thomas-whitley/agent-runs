"""GET /runs: metadata only, newest first, keyset pagination on (created_at, id).

start_server runs migrations through the app's lifespan, so it must start
before any direct insert against the runs table.
"""

import httpx2
import psycopg


def test_runs_are_listed_newest_first_with_metadata_only(start_server, clean_db):
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        oldest = conn.execute(
            "INSERT INTO runs (task, type, provider, status, created_at) "
            "VALUES ('x', 'pytest', 'gemini', 'pending', now() - interval '2 minutes') "
            "RETURNING id"
        ).fetchone()[0]
        newest = conn.execute(
            "INSERT INTO runs (task, type, provider, status, created_at) "
            "VALUES ('x', 'pytest', 'gemini', 'pending', now()) RETURNING id"
        ).fetchone()[0]

    response = httpx2.get(f"{base_url}/runs")

    assert response.status_code == 200
    body = response.json()
    assert [run["id"] for run in body["runs"]] == [str(newest), str(oldest)]
    run = body["runs"][0]
    assert set(run) == {
        "id",
        "type",
        "provider",
        "executor",
        "status",
        "tokens",
        "duration_seconds",
        "created_at",
    }


def test_duration_is_null_until_the_run_finishes(start_server, clean_db):
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        pending_id = conn.execute(
            "INSERT INTO runs (task, type, provider, status) "
            "VALUES ('x', 'pytest', 'gemini', 'pending') RETURNING id"
        ).fetchone()[0]
        finished_id = conn.execute(
            "INSERT INTO runs (task, type, provider, status, tokens_used, "
            "created_at, finished_at) "
            "VALUES ('x', 'pytest', 'gemini', 'succeeded', 42, "
            "now() - interval '10 seconds', now()) RETURNING id"
        ).fetchone()[0]

    body = httpx2.get(f"{base_url}/runs").json()
    by_id = {run["id"]: run for run in body["runs"]}

    assert by_id[str(pending_id)]["duration_seconds"] is None
    duration = by_id[str(finished_id)]["duration_seconds"]
    assert 8 <= duration <= 15, "a wall clock interval, not an exact one"
    assert by_id[str(finished_id)]["tokens"] == 42


def test_default_limit_is_fifty(start_server, clean_db):
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (task, created_at) "
            "SELECT 'x', now() - (n || ' seconds')::interval FROM generate_series(1, 60) AS n"
        )

    body = httpx2.get(f"{base_url}/runs").json()

    assert len(body["runs"]) == 50
    assert body["next_cursor"] is not None


def test_a_page_never_exceeds_two_hundred_rows(start_server, clean_db):
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (task, created_at) "
            "SELECT 'x', now() - (n || ' seconds')::interval FROM generate_series(1, 210) AS n"
        )

    body = httpx2.get(f"{base_url}/runs", params={"limit": 200}).json()

    assert len(body["runs"]) == 200
    assert body["next_cursor"] is not None


def test_a_cursor_page_boundary_where_two_runs_share_created_at(start_server, clean_db):
    """Ties on created_at are common: bulk inserts, or fast successive runs."""
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        ids = [
            conn.execute("INSERT INTO runs (task) VALUES ('x') RETURNING id").fetchone()[0]
            for _ in range(4)
        ]
        # Pin every row to the exact same timestamp so id is the only tiebreaker.
        conn.execute("UPDATE runs SET created_at = now()")

    first_page = httpx2.get(f"{base_url}/runs", params={"limit": 2}).json()
    assert len(first_page["runs"]) == 2
    assert first_page["next_cursor"] is not None

    second_page = httpx2.get(
        f"{base_url}/runs", params={"limit": 2, "cursor": first_page["next_cursor"]}
    ).json()

    seen = [run["id"] for run in first_page["runs"]] + [run["id"] for run in second_page["runs"]]
    assert sorted(seen) == sorted(str(i) for i in ids)
    assert len(seen) == len(set(seen)), "a run was repeated across the pages"


def test_the_last_page_carries_no_further_cursor(start_server, clean_db):
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("INSERT INTO runs (task) VALUES ('x')")

    body = httpx2.get(f"{base_url}/runs", params={"limit": 50}).json()

    assert len(body["runs"]) == 1
    assert body["next_cursor"] is None


def test_the_list_reads_type_and_provider_as_stored_without_validating_them(start_server, clean_db):
    """The endpoint reads whatever is in the row; POST /runs is what validates a type."""
    base_url = start_server()
    with psycopg.connect(clean_db, autocommit=True) as conn:
        run_id = conn.execute(
            "INSERT INTO runs (task, type, provider, executor, status) "
            "VALUES ('x', 'site_check', NULL, 'checks-1', 'pending') RETURNING id"
        ).fetchone()[0]

    body = httpx2.get(f"{base_url}/runs").json()

    run = next(run for run in body["runs"] if run["id"] == str(run_id))
    assert run["type"] == "site_check"
    assert run["provider"] is None
    assert run["executor"] == "checks-1"


def test_get_runs_rejects_a_limit_above_two_hundred(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/runs", params={"limit": 201})

    assert response.status_code == 422


def test_get_runs_rejects_a_limit_below_one(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/runs", params={"limit": 0})

    assert response.status_code == 422
