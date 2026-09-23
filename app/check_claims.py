"""The endpoints the self hosted checks worker uses, per docs/mercury.md's
"Two executors for one task type". Every one is behind the bearer token.

Claim only ever returns a site_check of a self hosted kind the worker
declared. The type filter lives in the SQL, not in a check after it, so no
other type can be handed out whatever the request says.
"""

import uuid

from fastapi import APIRouter, Depends, Request, Response
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


class ClaimRequest(BaseModel):
    worker_id: str
    kinds: list[str]

    @field_validator("worker_id")
    @classmethod
    def worker_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("worker_id must not be blank")
        return value

    @field_validator("kinds")
    @classmethod
    def kinds_must_be_self_hosted(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("declare at least one check kind")
        refused = sorted(set(value) - set(SELF_HOSTED_CHECK_KINDS))
        if refused:
            raise ValueError(f"not a self hosted check kind: {', '.join(refused)}")
        return value


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
