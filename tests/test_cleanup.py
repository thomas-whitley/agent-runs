"""The retention cleanup: event and step bodies older than 30 days go, a
check's summary stays a year, and a run row older than a year goes.
"""

from app.cleanup import run_cleanup
from app.runs import finish_run, record_step


def _run(conn, run_type: str, task: str, check_kind: str | None = None) -> str:
    return conn.execute(
        "INSERT INTO runs (task, type, check_kind) VALUES (%s, %s, %s) RETURNING id::text",
        (task, run_type, check_kind),
    ).fetchone()[0]


def _age(conn, run_id: str, days: int) -> None:
    conn.execute(
        "UPDATE runs SET created_at = now() - make_interval(days => %s),"
        " finished_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, days, run_id),
    )


def _pytest_run(conn, *, age_days: int) -> str:
    run_id = _run(conn, "pytest", "def test_secret(): assert solve() == 42")
    record_step(conn, run_id, 1, "act", input={"code": "def solve(): ..."}, output={"code": "x"})
    record_step(conn, run_id, 2, "done", output={"status": "succeeded", "code": "def solve()"})
    finish_run(conn, run_id, "succeeded", 120)
    _age(conn, run_id, age_days)
    return run_id


def _lighthouse_run(conn, *, age_days: int) -> str:
    run_id = _run(conn, "site_check", "https://example.com/", "lighthouse")
    summary = {"scores": {"performance": 0.91, "accessibility": 1.0}, "lcp_ms": 1840}
    record_step(conn, run_id, 1, "check", output=summary)
    record_step(conn, run_id, 2, "done", output={"status": "succeeded"})
    finish_run(conn, run_id, "succeeded", 0)
    _age(conn, run_id, age_days)
    return run_id


def _payloads(conn, run_id: str) -> list[dict]:
    rows = conn.execute("SELECT payload FROM events WHERE run_id = %s ORDER BY seq", (run_id,))
    return [row[0] for row in rows]


def test_a_check_summary_survives_the_cleanup_that_strips_a_pytest_run(migrated_db):
    old_pytest = _pytest_run(migrated_db, age_days=31)
    old_check = _lighthouse_run(migrated_db, age_days=31)

    run_cleanup(migrated_db)

    summary = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND seq = 1", (old_check,)
    ).fetchone()[0]
    assert summary == {"scores": {"performance": 0.91, "accessibility": 1.0}, "lcp_ms": 1840}
    assert _payloads(migrated_db, old_check)[0]["output"] == summary

    steps = migrated_db.execute(
        "SELECT input, output FROM steps WHERE run_id = %s ORDER BY seq", (old_pytest,)
    ).fetchall()
    assert steps == [(None, None), (None, {"status": "succeeded"})]
    assert migrated_db.execute("SELECT task FROM runs WHERE id = %s", (old_pytest,)).fetchone() == (
        "",
    )


def test_a_stripped_run_still_replays_its_steps_and_its_done_status(migrated_db):
    old_pytest = _pytest_run(migrated_db, age_days=31)

    run_cleanup(migrated_db)

    assert _payloads(migrated_db, old_pytest) == [
        {"seq": 1, "kind": "act"},
        {"seq": 2, "kind": "done", "output": {"status": "succeeded"}},
    ]


def test_a_run_inside_the_window_keeps_its_bodies(migrated_db):
    recent = _pytest_run(migrated_db, age_days=29)

    run_cleanup(migrated_db)

    assert _payloads(migrated_db, recent)[0]["output"] == {"code": "x"}
    assert migrated_db.execute("SELECT task FROM runs WHERE id = %s", (recent,)).fetchone()[0]


def test_an_unfinished_run_is_left_alone_however_old(migrated_db):
    run_id = _run(migrated_db, "pytest", "def test_x(): pass")
    record_step(migrated_db, run_id, 1, "act", output={"code": "x"})
    migrated_db.execute(
        "UPDATE runs SET created_at = now() - interval '40 days' WHERE id = %s", (run_id,)
    )

    run_cleanup(migrated_db)

    assert _payloads(migrated_db, run_id)[0]["output"] == {"code": "x"}


def test_the_cleanup_reports_what_it_did_and_a_second_pass_does_nothing(migrated_db):
    _pytest_run(migrated_db, age_days=31)
    _lighthouse_run(migrated_db, age_days=31)

    first = run_cleanup(migrated_db)
    second = run_cleanup(migrated_db)

    assert (first.runs_stripped, first.runs_deleted) == (1, 0)
    assert (second.runs_stripped, second.runs_deleted) == (0, 0)


def test_a_run_older_than_a_year_is_deleted_with_its_steps_and_events(migrated_db):
    ancient_check = _lighthouse_run(migrated_db, age_days=366)
    year_old_check = _lighthouse_run(migrated_db, age_days=364)

    result = run_cleanup(migrated_db)

    assert result.runs_deleted == 1
    remaining = {row[0] for row in migrated_db.execute("SELECT id::text FROM runs")}
    assert remaining == {year_old_check}
    assert migrated_db.execute(
        "SELECT count(*) FROM events WHERE run_id = %s", (ancient_check,)
    ).fetchone() == (0,)


def test_a_deleted_run_that_an_open_anyway_run_points_at_does_not_block_the_delete(migrated_db):
    source = _pytest_run(migrated_db, age_days=366)
    reopened = _pytest_run(migrated_db, age_days=200)
    migrated_db.execute("UPDATE runs SET source_run_id = %s WHERE id = %s", (source, reopened))

    run_cleanup(migrated_db)

    row = migrated_db.execute(
        "SELECT source_run_id FROM runs WHERE id = %s", (reopened,)
    ).fetchone()
    assert row == (None,)


def test_the_windows_come_from_the_arguments(migrated_db):
    run_id = _pytest_run(migrated_db, age_days=8)

    run_cleanup(migrated_db, event_bodies_days=7, runs_days=365)

    assert _payloads(migrated_db, run_id)[0] == {"seq": 1, "kind": "act"}
