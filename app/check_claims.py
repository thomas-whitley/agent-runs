"""The endpoints the self hosted checks worker uses, per docs/mercury.md's
"Two executors for one task type". Every one is behind the bearer token.

Claim only ever returns a site_check of a self hosted kind the worker
declared. The type filter lives in the SQL, not in a check after it, so no
other type can be handed out whatever the request says.
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, field_validator

from app.auth import require_bearer_token
from app.tasks import SELF_HOSTED_CHECK_KINDS

router = APIRouter(prefix="/checks", dependencies=[Depends(require_bearer_token)])

# Oldest first. SKIP LOCKED lets two workers claim at once without both
# getting the same check. A running check whose worker stopped reporting for
# longer than the lease is claimable again, the same takeover the Python
# worker uses.
_CLAIM = """
UPDATE runs
SET claimed_by = %s, status = 'running', heartbeat_at = now(), executor = 'self_hosted'
WHERE id = (
    SELECT id FROM runs
    WHERE type = 'site_check'
      AND check_kind = ANY(%s)
      AND finished_at IS NULL
      AND (
            claimed_by IS NULL
            OR (status = 'running' AND heartbeat_at < now() - make_interval(secs => %s))
          )
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, task, check_kind
"""

# Fenced on the worker that holds the claim and on the type, so this token
# cannot keep any run but its own site_check alive.
_HEARTBEAT = """
UPDATE runs
SET heartbeat_at = now()
WHERE id = %s
  AND type = 'site_check'
  AND claimed_by = %s
  AND finished_at IS NULL
"""

# Fenced like the heartbeat. A result is terminal whatever it says, so the
# run closes succeeded: a site that returned 500 is a finished check with a
# bad finding, not a reason to run it again elsewhere.
_CLOSE = """
UPDATE runs
SET status = 'succeeded', tokens_used = 0, finished_at = now()
WHERE id = %s
  AND type = 'site_check'
  AND claimed_by = %s
  AND finished_at IS NULL
"""

_INSERT_STEP = """
INSERT INTO steps (run_id, seq, kind, output, finished_at)
VALUES (%s, %s, %s, %s, now())
"""

_INSERT_EVENT = """
INSERT INTO events (run_id, seq, payload)
VALUES (%s, %s, %s)
"""

# A Lighthouse summary is about 1 KB. This leaves room for the crawl's
# findings and the log tail sent on failure, and refuses a full report.
MAX_RESULT_BYTES = 16_384


class WorkerRequest(BaseModel):
    worker_id: str

    @field_validator("worker_id")
    @classmethod
    def worker_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("worker_id must not be blank")
        return value


class ClaimRequest(WorkerRequest):
    kinds: list[str]

    @field_validator("kinds")
    @classmethod
    def kinds_must_be_self_hosted(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("declare at least one check kind")
        refused = sorted(set(value) - set(SELF_HOSTED_CHECK_KINDS))
        if refused:
            raise ValueError(f"not a self hosted check kind: {', '.join(refused)}")
        return value


class ResultRequest(WorkerRequest):
    result: dict[str, Any]

    @field_validator("result")
    @classmethod
    def result_must_be_a_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_RESULT_BYTES:
            raise ValueError(f"result is over {MAX_RESULT_BYTES} bytes; send a summary")
        return value


class Lease(BaseModel):
    lease_seconds: float


class ClaimedCheck(BaseModel):
    id: uuid.UUID
    url: str
    kind: str
    lease_seconds: float


@router.post("/claim", response_model=ClaimedCheck, responses={204: {"description": "nothing"}})
async def claim_check(claim: ClaimRequest, request: Request):
    lease_seconds = request.app.state.settings.lease_seconds
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(_CLAIM, (claim.worker_id, claim.kinds, lease_seconds))
        row = await cursor.fetchone()
    if row is None:
        return Response(status_code=204)
    return ClaimedCheck(id=row[0], url=row[1], kind=row[2], lease_seconds=lease_seconds)


@router.post("/{run_id}/heartbeat", response_model=Lease)
async def heartbeat_check(run_id: uuid.UUID, worker: WorkerRequest, request: Request) -> Lease:
    """409 means this worker no longer holds the check, so it must stop."""
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(_HEARTBEAT, (str(run_id), worker.worker_id))
    if cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail="this worker does not hold the check")
    return Lease(lease_seconds=request.app.state.settings.lease_seconds)


@router.post("/{run_id}/result")
async def post_check_result(
    run_id: uuid.UUID, posted: ResultRequest, request: Request
) -> dict[str, str]:
    """Close the check with its result as step 1 and a done event as step 2,
    in one transaction. 409 means this worker does not hold an open check."""
    run = str(run_id)
    done = {"status": "succeeded"}
    async with request.app.state.pool.connection() as conn:
        async with conn.transaction():
            cursor = await conn.execute(_CLOSE, (run, posted.worker_id))
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="this worker does not hold the check")
            for seq, kind, output in ((1, "check", posted.result), (2, "done", done)):
                await conn.execute(_INSERT_STEP, (run, seq, kind, Jsonb(output)))
                await conn.execute(
                    _INSERT_EVENT, (run, seq, Jsonb({"kind": kind, "seq": seq, "output": output}))
                )
    return done
