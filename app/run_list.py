"""GET /runs: metadata only, newest first, keyset pagination on (created_at, id).

No rate limiter exists on this endpoint or anywhere else in the API today;
the limit cap below is the only bound on how much one request can return.
"""

import base64
import binascii
import uuid
from datetime import datetime

from fastapi import HTTPException

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# created_at DESC, id DESC: newest first, with the run's own id (a uuid, not
# time ordered, but a fixed total order) breaking a tie on created_at.
LIST_RUNS = """
SELECT id, type, provider, executor, status, tokens_used,
       extract(epoch from (finished_at - created_at)) AS duration_seconds,
       created_at
FROM runs
{where}
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_CURSOR_CLAUSE = "WHERE (created_at, id) < (%s, %s)"


def serialize_run_row(row: tuple) -> dict:
    run_id, type_, provider, executor, status, tokens_used, duration_seconds, created_at = row
    return {
        "id": str(run_id),
        "type": type_,
        "provider": provider,
        "executor": executor,
        "status": status,
        "tokens": tokens_used,
        # extract(epoch from ...) comes back as a Decimal; float, not the
        # default jsonable_encoder stringification, is what a client wants.
        "duration_seconds": float(duration_seconds) if duration_seconds is not None else None,
        "created_at": created_at.isoformat(),
    }


def encode_cursor(created_at: datetime, run_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{run_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_text, run_id = raw.split("|", 1)
        return datetime.fromisoformat(created_at_text), run_id
    except (ValueError, binascii.Error) as error:
        raise HTTPException(status_code=422, detail="invalid cursor") from error


def build_query(cursor: str | None) -> tuple[str, list]:
    """The SQL and its parameters, minus the trailing limit."""
    if cursor is None:
        return LIST_RUNS.format(where=""), []
    created_at, run_id = decode_cursor(cursor)
    return LIST_RUNS.format(where=_CURSOR_CLAUSE), [created_at, run_id]
