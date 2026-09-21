"""Writes against the runs, steps and events tables.

Every write here is safe to retry. The worker can die at any point and the
replacement repeats the same step with the same seq without doubling anything.
"""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

_CLAIM = """
UPDATE runs
SET claimed_by = %s, status = 'running'
WHERE id = %s AND claimed_by IS NULL
"""

_INSERT_STEP = """
INSERT INTO steps (run_id, seq, kind, input, output, tokens, finished_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (run_id, seq) DO NOTHING
"""

_INSERT_EVENT = """
INSERT INTO events (run_id, seq, payload)
VALUES (%s, %s, %s)
ON CONFLICT (run_id, seq) DO NOTHING
"""


def claim_run(conn: psycopg.Connection, run_id: str, worker_id: str) -> bool:
    """Take ownership of a run. False means another worker already has it."""
    cursor = conn.execute(_CLAIM, (worker_id, run_id))
    return cursor.rowcount == 1


def record_step(
    conn: psycopg.Connection,
    run_id: str,
    seq: int,
    kind: str,
    *,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    tokens: int = 0,
) -> bool:
    """Write one step and the event that announces it, in one transaction.

    False means this seq was already committed, so nothing was written.
    """
    payload = {"kind": kind, "seq": seq, "output": output}

    with conn.transaction():
        cursor = conn.execute(
            _INSERT_STEP,
            (
                run_id,
                seq,
                kind,
                Jsonb(input) if input is not None else None,
                Jsonb(output) if output is not None else None,
                tokens,
            ),
        )
        if cursor.rowcount == 0:
            return False
        conn.execute(_INSERT_EVENT, (run_id, seq, Jsonb(payload)))

    return True
