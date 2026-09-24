"""The worker's KEDA query in infra/main.bicep, run against a real database.

The worker scales to zero, so this query is the only thing that wakes it. If
it misses an overdue Lighthouse check, the cloud fallback never runs, and if
it counts a check still inside its window, the worker sits awake for the
whole window every week.
"""

import pathlib
import re

import pytest

BICEP = pathlib.Path(__file__).resolve().parent.parent / "infra" / "main.bicep"
WINDOW = 600
LEASE = 120


def keda_query() -> str:
    source = BICEP.read_text()
    match = re.search(r"^var pendingWorkQuery = '(.*)'$", source, re.MULTILINE)
    assert match, "pendingWorkQuery not found in main.bicep"
    query = match.group(1).replace("\\'", "'")
    query = query.replace("${checkClaimWindowSeconds}", str(WINDOW))
    query = query.replace("${leaseSeconds + checkClaimWindowSeconds}", str(LEASE + WINDOW))
    assert "${" not in query, f"an interpolation this test does not fill: {query}"
    return query


def lease_in_bicep() -> int:
    match = re.search(r"^var leaseSeconds = (\d+)$", BICEP.read_text(), re.MULTILINE)
    assert match
    return int(match.group(1))


def pending_work(conn) -> int:
    return conn.execute(keda_query()).fetchone()[0]


def add_check(conn, kind="lighthouse", age="0 seconds", **columns) -> str:
    run_id = conn.execute(
        "INSERT INTO runs (task, type, check_kind, created_at) "
        "VALUES ('https://example.com', 'site_check', %s, now() - %s::interval) RETURNING id",
        (kind, age),
    ).fetchone()[0]
    for column, value in columns.items():
        conn.execute(f"UPDATE runs SET {column} = %s WHERE id = %s", (value, run_id))
    return run_id


def test_the_bicep_lease_matches_the_app_default():
    from app.config import DEFAULT_LEASE_SECONDS

    assert lease_in_bicep() == DEFAULT_LEASE_SECONDS


def test_a_pending_pytest_run_wakes_the_worker(migrated_db):
    migrated_db.execute("INSERT INTO runs (task) VALUES ('x')")

    assert pending_work(migrated_db) == 1


def test_a_lighthouse_check_inside_its_window_does_not(migrated_db):
    add_check(migrated_db, age="9 minutes")

    assert pending_work(migrated_db) == 0


def test_a_lighthouse_check_past_its_window_does(migrated_db):
    add_check(migrated_db, age="11 minutes")

    assert pending_work(migrated_db) == 1


def test_a_check_the_cloud_is_running_keeps_the_worker_up(migrated_db):
    add_check(
        migrated_db, age="11 minutes", claimed_by="worker", status="running", executor="cloud"
    )

    assert pending_work(migrated_db) == 1


def test_a_check_the_laptop_is_running_does_not_wake_it(migrated_db):
    add_check(
        migrated_db, age="1 hour", claimed_by="laptop", status="running", executor="self_hosted"
    )
    migrated_db.execute("UPDATE runs SET heartbeat_at = now() - interval '30 seconds'")

    assert pending_work(migrated_db) == 0


@pytest.mark.parametrize(("minutes", "expected"), [(5, 0), (13, 1)])
def test_a_lapsed_laptop_lease_wakes_it_only_after_the_window(migrated_db, minutes, expected):
    add_check(
        migrated_db, age="1 hour", claimed_by="laptop", status="running", executor="self_hosted"
    )
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - make_interval(mins => %s)", (minutes,)
    )

    assert pending_work(migrated_db) == expected


@pytest.mark.parametrize("kind", ["uptime", "broken_links"])
def test_checks_the_cloud_never_runs_never_wake_it(migrated_db, kind):
    add_check(migrated_db, kind=kind, age="1 day")

    assert pending_work(migrated_db) == 0
