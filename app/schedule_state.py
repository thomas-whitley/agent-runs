"""Per-schedule failure tracking. Three consecutive failures suspends a
schedule entry until something resumes it, so a broken site does not retry
every hour forever and burn the workspace's daily log ingestion cap.
"""

import psycopg

SUSPEND_AFTER = 3

_ENSURE_ROW = "INSERT INTO schedule_state (name) VALUES (%s) ON CONFLICT (name) DO NOTHING"

_READ_SUSPENDED = "SELECT suspended FROM schedule_state WHERE name = %s"

_RECORD_FAILURE = """
UPDATE schedule_state
SET consecutive_failures = consecutive_failures + 1,
    suspended = (consecutive_failures + 1) >= %s,
    suspended_at = CASE
        WHEN (consecutive_failures + 1) >= %s THEN now()
        ELSE suspended_at
    END
WHERE name = %s
RETURNING consecutive_failures = %s
"""

_RECORD_SUCCESS = """
UPDATE schedule_state SET consecutive_failures = 0
WHERE name = %s AND suspended = false
"""

RESUME = """
UPDATE schedule_state
SET suspended = false, consecutive_failures = 0, suspended_at = NULL
WHERE name = %s
"""


def is_suspended(conn: psycopg.Connection, name: str) -> bool:
    conn.execute(_ENSURE_ROW, (name,))
    row = conn.execute(_READ_SUSPENDED, (name,)).fetchone()
    return bool(row[0]) if row else False


def record_failure(conn: psycopg.Connection, name: str) -> bool:
    """Count a failure. True when this one suspended the schedule, so the
    caller sends the suspension message once and not every hour after."""
    conn.execute(_ENSURE_ROW, (name,))
    row = conn.execute(
        _RECORD_FAILURE, (SUSPEND_AFTER, SUSPEND_AFTER, name, SUSPEND_AFTER)
    ).fetchone()
    return bool(row[0])


def record_success(conn: psycopg.Connection, name: str) -> None:
    conn.execute(_ENSURE_ROW, (name,))
    conn.execute(_RECORD_SUCCESS, (name,))


def resume(conn: psycopg.Connection, name: str) -> None:
    conn.execute(_ENSURE_ROW, (name,))
    conn.execute(RESUME, (name,))
