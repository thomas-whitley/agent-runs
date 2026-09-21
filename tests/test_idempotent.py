"""A retried step that already committed must be a no-op."""

from app.runs import claim_run, record_step


def new_run(conn, task: str = "make the test pass") -> str:
    return conn.execute("INSERT INTO runs (task) VALUES (%s) RETURNING id", (task,)).fetchone()[0]


def test_only_one_worker_claims_a_run(migrated_db):
    run_id = new_run(migrated_db)

    assert claim_run(migrated_db, run_id, "worker-a") is True
    assert claim_run(migrated_db, run_id, "worker-b") is False

    row = migrated_db.execute(
        "SELECT claimed_by, status FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("worker-a", "running")


def test_a_repeated_step_writes_one_row_and_one_event(migrated_db):
    run_id = new_run(migrated_db)

    assert record_step(migrated_db, run_id, 1, "plan", output={"text": "first"}) is True
    assert record_step(migrated_db, run_id, 1, "plan", output={"text": "first"}) is False

    steps = migrated_db.execute(
        "SELECT count(*) FROM steps WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    events = migrated_db.execute(
        "SELECT count(*) FROM events WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    assert (steps, events) == (1, 1)


def test_a_step_and_its_event_are_written_together(migrated_db):
    """The event carries the same seq as the step, so neither can exist alone."""
    run_id = new_run(migrated_db)

    record_step(migrated_db, run_id, 1, "plan", output={"text": "a"})
    record_step(migrated_db, run_id, 2, "verify", output={"passed": False})

    rows = migrated_db.execute(
        "SELECT s.seq, e.payload ->> 'kind' FROM steps s "
        "JOIN events e ON e.run_id = s.run_id AND e.seq = s.seq "
        "WHERE s.run_id = %s ORDER BY s.seq",
        (run_id,),
    ).fetchall()

    assert rows == [(1, "plan"), (2, "verify")]


def test_a_later_step_still_writes_after_a_retried_one(migrated_db):
    run_id = new_run(migrated_db)

    record_step(migrated_db, run_id, 1, "plan", output={"text": "a"})
    record_step(migrated_db, run_id, 1, "plan", output={"text": "a"})

    assert record_step(migrated_db, run_id, 2, "act", output={"code": "x"}) is True

    seqs = [
        row[0]
        for row in migrated_db.execute(
            "SELECT seq FROM events WHERE run_id = %s ORDER BY seq", (run_id,)
        ).fetchall()
    ]
    assert seqs == [1, 2]
