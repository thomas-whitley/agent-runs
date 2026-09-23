"""The scheduler: creates a site_check run through the API with the bearer
token, checks the site itself in the same process, writes the result, and
tracks consecutive failures per schedule entry.
"""

import logging

from app.schedule_state import is_suspended, record_failure
from app.scheduler import run_due_checks

BEARER_TOKEN = "test-bearer-token"
# Nothing listens on port 1, so a check against it fails at once.
UNREACHABLE = "http://127.0.0.1:1"


def test_run_due_checks_creates_a_run_and_writes_its_result(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()

    created = run_due_checks(migrated_db, (f"{base_url}/health",), base_url, BEARER_TOKEN)

    assert len(created) == 1
    run_id = created[0]
    row = migrated_db.execute(
        "SELECT type, status, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("site_check", "succeeded", 0)

    output = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'check'", (run_id,)
    ).fetchone()[0]
    assert output["passed"] is True
    assert output["status_code"] == 200


def test_run_due_checks_records_a_failed_check(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()

    created = run_due_checks(migrated_db, (UNREACHABLE,), base_url, BEARER_TOKEN)

    row = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (created[0],)).fetchone()
    assert row == ("failed",)


def test_run_due_checks_skips_a_suspended_site(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    for _ in range(3):
        record_failure(migrated_db, f"site_uptime:{UNREACHABLE}")

    created = run_due_checks(migrated_db, (UNREACHABLE,), base_url, BEARER_TOKEN)

    assert created == []


def test_run_due_checks_suspends_after_the_third_consecutive_failure(
    start_server, migrated_db, monkeypatch
):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    name = f"site_uptime:{UNREACHABLE}"

    run_due_checks(migrated_db, (UNREACHABLE,), base_url, BEARER_TOKEN)
    run_due_checks(migrated_db, (UNREACHABLE,), base_url, BEARER_TOKEN)
    assert is_suspended(migrated_db, name) is False

    run_due_checks(migrated_db, (UNREACHABLE,), base_url, BEARER_TOKEN)

    assert is_suspended(migrated_db, name) is True


def test_run_due_checks_counts_a_refused_post_as_a_failure(start_server, migrated_db, monkeypatch):
    """A wrong or expired token must suspend the schedule, not skip it
    quietly every hour with nothing to show that checks have stopped."""
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    site = f"{base_url}/health"

    for _ in range(3):
        created = run_due_checks(migrated_db, (site,), base_url, "wrong-token")
        assert created == []

    assert is_suspended(migrated_db, f"site_uptime:{site}") is True


def test_run_due_checks_logs_the_created_run_with_no_client(
    start_server, migrated_db, monkeypatch, caplog
):
    """The exact line the live deploy's exit condition greps for.

    create_app() calls configure_logging(), which replaces the root handlers
    and caplog's with them, so the handler goes on the scheduler's logger.
    """
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    scheduler_logger = logging.getLogger("agent_runs.scheduler")
    scheduler_logger.addHandler(caplog.handler)

    try:
        with caplog.at_level("INFO", logger="agent_runs.scheduler"):
            created = run_due_checks(migrated_db, (f"{base_url}/health",), base_url, BEARER_TOKEN)
    finally:
        scheduler_logger.removeHandler(caplog.handler)

    assert f"scheduled run {created[0]} created with no client" in caplog.text
