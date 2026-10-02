"""An MCP server at /mcp, inside the API, for Claude Code.

Streamable HTTP from the official `mcp` SDK, stateless with JSON responses,
so any replica answers any request and nothing is held between calls. Every
tool calls the same functions as the HTTP routes (app/run_api.py) and the
Telegram commands, so the registry, the caps and the chore gate apply
unchanged. Runs created here are written source mcp.

There is no approve tool. A chore created here waits for the Approve button
on Telegram like any other, which is the point: the owner's phone stays the
only place a chore starts.
"""

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.run_api import create_run, get_run, list_runs, run_events
from app.run_list import DEFAULT_LIMIT, MAX_LIMIT
from app.run_request import RunRequest
from app.telegram_webhook import cancel_by_prefix, status_text

CREATE_RUN = """Queue a Mercury run. type is one of pytest (task is a complete pytest \
file), site_check (task is a URL, kind is uptime, lighthouse or broken_links) or \
repo_chore (task is an instruction, repo is owner/name from the configured portfolio). \
A repo_chore does not start here: it waits for the owner to press Approve on Telegram, \
and this server has no way to approve it. Tell the user to check Telegram. Returns the \
run id and its status."""


def _refused(error: HTTPException) -> ToolError:
    """A refusal the HTTP route would answer with this status, as a tool
    error whose text reaches the client. Any other exception's text stays on
    the server, which the SDK does on purpose."""
    return ToolError(f"{error.status_code}: {error.detail}")


def build_mcp(app: FastAPI) -> MCPServer:
    """The tools read the pool, settings and config from app.state at call time."""
    mcp = MCPServer(
        name="mercury",
        instructions="Queue and read Mercury runs. Repo chores need approval on Telegram.",
    )

    @mcp.tool(name="create_run", description=CREATE_RUN)
    async def create_run_tool(
        type: str, task: str, kind: str | None = None, repo: str | None = None
    ) -> dict[str, Any]:
        inputs: dict[str, Any] = {"task": task}
        if kind is not None:
            inputs["kind"] = kind
        if repo is not None:
            inputs["repo"] = repo
        try:
            run = RunRequest(type=type, inputs=inputs)
            created = await create_run(app.state, run, "mcp")
        except ValidationError as error:
            raise ToolError(f"422: {error.errors()[0]['msg']}") from None
        except HTTPException as error:
            raise _refused(error) from None
        return created.model_dump()

    @mcp.tool(
        name="list_runs", description="List runs newest first, with the same fields as GET /runs."
    )
    async def list_runs_tool(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
        try:
            return await list_runs(app.state.pool, limit, cursor)
        except HTTPException as error:
            raise _refused(error) from None

    @mcp.tool(
        name="get_run",
        description="Read one run by its id: type, source, status, tokens and timing.",
    )
    async def get_run_tool(run_id: str) -> dict[str, Any]:
        try:
            return await get_run(app.state.pool, run_id)
        except HTTPException as error:
            raise _refused(error) from None

    @mcp.tool(
        name="get_run_events",
        description="A run's events so far with their bodies, oldest first. Pass after "
        "as the last event id you have to read only newer ones.",
    )
    async def get_run_events_tool(run_id: str, after: int = 0) -> dict[str, Any]:
        try:
            return {"events": await run_events(app.state.pool, run_id, after)}
        except HTTPException as error:
            raise _refused(error) from None

    @mcp.tool(
        name="cancel_run",
        description="Cancel an unfinished run by its id or its first 8 characters.",
    )
    async def cancel_run_tool(run_id: str) -> str:
        return await cancel_by_prefix(app.state.pool, run_id.strip().lower(), by="MCP")

    @mcp.tool(name="status", description="Each site's last uptime check and today's run counts.")
    async def status_tool() -> str:
        return await status_text(
            app.state.pool, app.state.mercury.sites, app.state.settings.max_runs_per_day
        )

    return mcp


class _RequireBearer:
    """The same check as app.auth.require_bearer_token, in front of the MCP
    ASGI app, which is not a FastAPI route and has no Request to hand it."""

    def __init__(self, app: FastAPI, inner) -> None:
        self.app = app
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:
        expected = self.app.state.settings.mercury_bearer_token
        headers = dict(scope.get("headers") or [])
        scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
            response = JSONResponse({"detail": "missing or invalid bearer token"}, 401)
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)


def mount_mcp(app: FastAPI) -> MCPServer:
    """Add /mcp to app's routes. Its session manager must run for the life of
    the app, which the lifespan in app/main.py does."""
    mcp = build_mcp(app)
    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # The SDK guards localhost servers against DNS rebinding by checking
        # Host. This one is public behind a bearer token, which a rebinding
        # page cannot send, and its Host is the Container Apps name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    [route] = [r for r in starlette_app.routes if isinstance(r, Route) and r.path == "/mcp"]
    app.router.routes.append(Route("/mcp", endpoint=_RequireBearer(app, route.endpoint)))
    return mcp
