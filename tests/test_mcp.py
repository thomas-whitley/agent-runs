"""The MCP server at /mcp: a real MCP client session against the app on a
real socket. Every tool sits behind the bearer token, calls the same code as
the HTTP routes, and writes runs with source mcp. There is no approve tool,
so a chore created here waits for the Telegram button like any other.
"""

import asyncio
import json
from typing import Any

import httpx2
import pytest
import yaml
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

CHAT = 42
TOKEN = "test-bearer-token"
REPO = "thomas-whitley/mercury-fixture"
TEST_FILE = "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
TOOLS = {"create_run", "list_runs", "get_run", "get_run_events", "cancel_run", "status"}


@pytest.fixture
def api(start_server, fake_telegram, monkeypatch, tmp_path):
    config = tmp_path / "mercury.yaml"
    config.write_text(
        yaml.dump(
            {
                "telegram": {"chat_id": CHAT},
                "portfolio": {
                    "sites": ["https://example.com/health"],
                    "repos": [{"name": REPO, "test_command": "uv run pytest"}],
                },
            }
        )
    )
    monkeypatch.setenv("MERCURY_CONFIG_PATH", str(config))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", TOKEN)
    return start_server()


def session(base_url: str, calls) -> Any:
    """Open one MCP session with the bearer token, run calls(session), return its result."""

    async def go():
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with streamable_http_client(f"{base_url}/mcp", http_client=http) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    return await calls(mcp)

    return asyncio.run(go())


def body(result) -> Any:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        # A tool returning a non-object is wrapped as {"result": ...}.
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result.content[0].text)


def call(base_url: str, name: str, **arguments) -> Any:
    async def calls(mcp):
        return body(await mcp.call_tool(name, arguments))

    return session(base_url, calls)


def test_the_tools_are_the_six_and_none_of_them_approves(api):
    async def calls(mcp):
        return (await mcp.list_tools()).tools

    tools = session(api, calls)

    assert {tool.name for tool in tools} == TOOLS
    assert not [tool for tool in tools if "approv" in tool.name]
    [create] = [tool for tool in tools if tool.name == "create_run"]
    # So the client tells the user where the approval happens.
    assert "Telegram" in create.description
    assert "approv" in create.description.lower()


def test_a_run_created_over_mcp_reads_back_with_source_mcp(api, migrated_db):
    created = call(api, "create_run", type="pytest", task=TEST_FILE)

    run = call(api, "get_run", run_id=created["id"])
    listed = call(api, "list_runs")
    assert created["status"] == "pending"
    assert run["source"] == "mcp"
    assert run["type"] == "pytest"
    assert [r["id"] for r in listed["runs"]] == [created["id"]]
    assert httpx2.get(f"{api}/runs/{created['id']}").json()["source"] == "mcp"


def test_a_chore_created_over_mcp_waits_for_approval(api, fake_telegram, migrated_db):
    created = call(api, "create_run", type="repo_chore", task="Add a test", repo=REPO)

    assert created["status"] == "awaiting_approval"
    assert migrated_db.execute("SELECT status, source FROM runs").fetchone() == (
        "awaiting_approval",
        "mcp",
    )
    [question] = fake_telegram.sent()
    labels = [b["text"] for r in question["reply_markup"]["inline_keyboard"] for b in r]
    assert labels == ["Approve", "Decline"]


def test_a_refused_run_is_a_tool_error_and_creates_nothing(api, migrated_db):
    async def calls(mcp):
        return await mcp.call_tool(
            "create_run", {"type": "repo_chore", "task": "x", "repo": "someone/else"}
        )

    result = session(api, calls)

    assert result.is_error
    assert REPO in result.content[0].text
    assert migrated_db.execute("SELECT count(*) FROM runs").fetchone() == (0,)


def test_cancel_ends_the_run_and_its_events_say_so(api, migrated_db):
    created = call(api, "create_run", type="pytest", task=TEST_FILE)

    cancelled = call(api, "cancel_run", run_id=created["id"])
    events = call(api, "get_run_events", run_id=created["id"])

    assert created["id"][:8] in cancelled
    assert [e["kind"] for e in events["events"]] == ["done"]
    assert events["events"][0]["output"] == {"status": "cancelled"}


def test_status_is_the_same_text_as_telegram_status(api, migrated_db):
    text = call(api, "status")

    assert "https://example.com/health: not checked yet" in text
    assert "Runs today: 0 of" in text


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic dGVzdA=="])
def test_a_request_without_the_token_is_refused(api, header):
    headers = {"Accept": "application/json, text/event-stream"}
    if header:
        headers["Authorization"] = header
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }

    response = httpx2.post(f"{api}/mcp", json=initialize, headers=headers)

    assert response.status_code == 401
