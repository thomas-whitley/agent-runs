"""A retried step that already committed must be a no-op."""

from app.runs import claim_run, finish_run, heartbeat, record_step


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


def test_a_fresh_claim_is_not_stolen(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")

    assert claim_run(migrated_db, run_id, "worker-b", lease_seconds=60) is False

    claimed_by = migrated_db.execute(
        "SELECT claimed_by FROM runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert claimed_by == "worker-a"


def test_a_claim_whose_worker_stopped_reporting_is_reclaimed(migrated_db):
    """A worker killed outright leaves a run claimed. Its lease must expire."""
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )

    assert claim_run(migrated_db, run_id, "worker-b", lease_seconds=60) is True

    claimed_by = migrated_db.execute(
        "SELECT claimed_by FROM runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert claimed_by == "worker-b"


def test_a_heartbeat_keeps_a_claim_alive(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )

    heartbeat(migrated_db, run_id, "worker-a")

    assert claim_run(migrated_db, run_id, "worker-b", lease_seconds=60) is False


def test_a_heartbeat_from_another_worker_does_nothing(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")

    assert heartbeat(migrated_db, run_id, "worker-b") is False
    assert heartbeat(migrated_db, run_id, "worker-a") is True


def test_a_finished_run_is_never_reclaimed(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")
    migrated_db.execute(
        "UPDATE runs SET status = 'succeeded', finished_at = now(), "
        "heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
        (run_id,),
    )

    assert claim_run(migrated_db, run_id, "worker-b", lease_seconds=60) is False


def test_a_worker_that_lost_its_claim_cannot_write_a_step(migrated_db):
    """A slow model call can outlast the lease. The loser must not keep writing."""
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )
    assert claim_run(migrated_db, run_id, "worker-b", lease_seconds=60) is True

    rejected = record_step(
        migrated_db, run_id, 1, "act", output={"code": "stale"}, worker_id="worker-a"
    )

    assert rejected is False
    assert (
        migrated_db.execute("SELECT count(*) FROM steps WHERE run_id = %s", (run_id,)).fetchone()[0]
        == 0
    )
    assert (
        migrated_db.execute("SELECT count(*) FROM events WHERE run_id = %s", (run_id,)).fetchone()[
            0
        ]
        == 0
    )


def test_the_new_owner_writes_normally(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-b")

    assert (
        record_step(migrated_db, run_id, 1, "act", output={"code": "fresh"}, worker_id="worker-b")
        is True
    )


def test_a_worker_that_lost_its_claim_cannot_finish_the_run(migrated_db):
    run_id = new_run(migrated_db)
    claim_run(migrated_db, run_id, "worker-a")
    migrated_db.execute(
        "UPDATE runs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (run_id,)
    )
    claim_run(migrated_db, run_id, "worker-b", lease_seconds=60)

    assert finish_run(migrated_db, run_id, "failed", 999, worker_id="worker-a") is False
    assert finish_run(migrated_db, run_id, "succeeded", 10, worker_id="worker-b") is True

    row = migrated_db.execute(
        "SELECT status, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("succeeded", 10), "the loser's status overwrote the owner's"


def test_an_unfenced_write_still_works_for_callers_without_a_worker_id(migrated_db):
    """The tests and any single worker path do not have to pass one."""
    run_id = new_run(migrated_db)

    assert record_step(migrated_db, run_id, 1, "plan", output={"text": "a"}) is True
    assert finish_run(migrated_db, run_id, "succeeded", 5) is True
