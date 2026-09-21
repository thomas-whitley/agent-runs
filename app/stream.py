"""The SSE view of the events table.

The events table is the whole cursor. Nothing about a connected client is held
server side, which is what lets a reconnect land on any replica.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from psycopg_pool import AsyncConnectionPool

POLL_SECONDS = 0.05

_SELECT_NEW_EVENTS = """
SELECT id, payload
FROM events
WHERE run_id = %s AND id > %s
ORDER BY id
"""

_IS_FINISHED = "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s"


def parse_last_event_id(header: str | None) -> int:
    """A missing or unreadable Last-Event-ID means replay the run from the start."""
    if not header:
        return 0
    try:
        return max(0, int(header.strip()))
    except ValueError:
        return 0


def format_event(event_id: int, payload: dict[str, Any]) -> str:
    """One event, one string, so a write is never split across two events."""
    name = "done" if payload.get("kind") == "done" else "step"
    return f"id: {event_id}\nevent: {name}\ndata: {json.dumps(payload)}\n\n"


async def event_stream(
    pool: AsyncConnectionPool,
    run_id: uuid.UUID,
    after_id: int,
    keepalive_seconds: float,
) -> AsyncIterator[str]:
    cursor_id = after_id
    idle_seconds = 0.0

    while True:
        async with pool.connection() as conn:
            cursor = await conn.execute(_SELECT_NEW_EVENTS, (str(run_id), cursor_id))
            rows = await cursor.fetchall()

            if not rows:
                # A cursor at or past the done event has nothing left coming. The
                # loop would otherwise poll and send keepalives for as long as the
                # client waited, which holds a replica above zero. The worker
                # writes the done event before it sets finished_at, so this cannot
                # close a stream that still has an event to deliver.
                cursor = await conn.execute(_IS_FINISHED, (str(run_id),))
                finished = await cursor.fetchone()
                if finished and finished[0]:
                    return

        if rows:
            idle_seconds = 0.0
            for event_id, payload in rows:
                cursor_id = event_id
                yield format_event(event_id, payload)
                if payload.get("kind") == "done":
                    return
            continue

        await asyncio.sleep(POLL_SECONDS)
        idle_seconds += POLL_SECONDS
        if idle_seconds >= keepalive_seconds:
            idle_seconds = 0.0
            yield ": keepalive\n\n"
