"""Retention, run by the scheduler Job on every tick.

A finished run older than event_bodies_days loses its bodies: the task input,
every step's input and output, and every event's output, so a reconnect a month
later still replays each step's seq and kind and the done event's status, the
same shape a caller with no token sees (app/stream.py without_body). A
site_check keeps everything, because its step 1 is the summary the digest
compares week over week. A run older than runs_days goes, steps and events
with it.

Both statements only touch rows that still have something to remove, so a
second pass in the same hour finds nothing.
"""

import logging
from dataclasses import dataclass

import psycopg

logger = logging.getLogger("agent_runs.cleanup")

EVENT_BODIES_DAYS = 30
RUNS_DAYS = 365

# The runs past the window that still hold a body. task = '' marks one done,
# since every step and event of the run is stripped in the same transaction.
_STRIP_RUNS = """
UPDATE runs SET task = ''
WHERE type <> 'site_check'
  AND finished_at < now() - make_interval(days => %s)
  AND task <> ''
RETURNING id
"""

_STRIP_STEPS = """
UPDATE steps SET
    input = NULL,
    output = CASE WHEN kind = 'done'
                  THEN jsonb_build_object('status', output -> 'status')
                  ELSE NULL END
WHERE run_id = ANY(%s)
"""

_STRIP_EVENTS = """
UPDATE events SET payload =
    jsonb_build_object('seq', payload -> 'seq', 'kind', payload -> 'kind')
    || CASE WHEN payload ->> 'kind' = 'done'
            THEN jsonb_build_object('output', jsonb_build_object(
                     'status', coalesce(payload -> 'output' -> 'status', payload -> 'status')))
            ELSE '{}'::jsonb END
WHERE run_id = ANY(%s)
"""

# An open anyway run points at the run it reapplied. The source is hours
# older, so it can pass the year first, and the reference is dropped rather
# than blocking the delete.
_DELETE_RUNS = """
WITH doomed AS (
    SELECT id FROM runs WHERE created_at < now() - make_interval(days => %s)
), unlinked AS (
    UPDATE runs SET source_run_id = NULL WHERE source_run_id IN (SELECT id FROM doomed)
)
DELETE FROM runs WHERE id IN (SELECT id FROM doomed)
"""


@dataclass(frozen=True)
class CleanupResult:
    runs_stripped: int
    runs_deleted: int


def run_cleanup(
    conn: psycopg.Connection,
    event_bodies_days: int = EVENT_BODIES_DAYS,
    runs_days: int = RUNS_DAYS,
) -> CleanupResult:
    with conn.transaction():
        stripped = [row[0] for row in conn.execute(_STRIP_RUNS, (event_bodies_days,))]
        if stripped:
            conn.execute(_STRIP_STEPS, (stripped,))
            conn.execute(_STRIP_EVENTS, (stripped,))
        deleted = conn.execute(_DELETE_RUNS, (runs_days,)).rowcount

    result = CleanupResult(runs_stripped=len(stripped), runs_deleted=deleted)
    if stripped or deleted:
        logger.info(
            "cleanup stripped the bodies of %s run(s) and deleted %s run(s)",
            result.runs_stripped,
            result.runs_deleted,
        )
    return result
