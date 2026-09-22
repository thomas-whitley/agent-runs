"""Bearer token check for every non-public endpoint, per docs/mercury.md's
guard list: "The bearer token on every non public endpoint."
"""

from fastapi import HTTPException, Request


def require_bearer_token(request: Request) -> None:
    """Raise 401 unless Authorization: Bearer <token> matches the configured
    one. An unconfigured token refuses every caller rather than waving them
    through, so a deploy that forgot MERCURY_BEARER_TOKEN fails closed."""
    expected = request.app.state.settings.mercury_bearer_token
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if not expected or scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
