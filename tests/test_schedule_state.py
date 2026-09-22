"""Three consecutive failures suspends one schedule entry until something
resumes it, so a broken site does not retry every hour forever and burn
the workspace's daily log ingestion cap.
"""

from app.schedule_state import is_suspended, record_failure, record_success, resume

NAME = "site_uptime:https://a.example"


def test_a_schedule_is_not_suspended_before_any_failure(migrated_db):
    assert is_suspended(migrated_db, NAME) is False


def test_three_consecutive_failures_suspends(migrated_db):
    record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is False
    record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is False
    record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True


def test_a_success_resets_the_streak_before_suspension(migrated_db):
    record_failure(migrated_db, NAME)
    record_failure(migrated_db, NAME)
    record_success(migrated_db, NAME)
    record_failure(migrated_db, NAME)
    record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is False


def test_resume_clears_a_suspension(migrated_db):
    for _ in range(3):
        record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is True

    resume(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is False


def test_a_success_after_suspension_does_not_auto_resume(migrated_db):
    """Only an explicit resume clears it, so a flapping site cannot self heal silently."""
    for _ in range(3):
        record_failure(migrated_db, NAME)

    record_success(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True


def test_two_schedule_names_are_independent(migrated_db):
    other = "site_uptime:https://b.example"
    for _ in range(3):
        record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True
    assert is_suspended(migrated_db, other) is False
