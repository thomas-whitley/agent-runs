"""Every run records where it came from: telegram, mcp, n8n, api or
scheduler. A caller of POST /runs may name api, n8n or scheduler. telegram
and mcp are set inside the app, so a POST naming either is refused.
"""

import httpx2
import pytest

TEST_FILE = "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def api(start_server, auth_headers):
    return start_server()


def post(base_url: str, headers: dict, **extra) -> httpx2.Response:
    return httpx2.post(
        f"{base_url}/runs",
        json={"type": "pytest", "inputs": {"task": TEST_FILE}, **extra},
        headers=headers,
    )


def source_of(base_url: str, run_id: str) -> str:
    return httpx2.get(f"{base_url}/runs/{run_id}").json()["source"]


def test_a_run_posted_with_no_source_is_api(api, auth_headers):
    run_id = post(api, auth_headers).json()["id"]

    assert source_of(api, run_id) == "api"


@pytest.mark.parametrize("source", ["api", "n8n", "scheduler"])
def test_a_caller_may_name_its_source(api, auth_headers, source):
    run_id = post(api, auth_headers, source=source).json()["id"]

    assert source_of(api, run_id) == source


@pytest.mark.parametrize("source", ["telegram", "mcp", "someone"])
def test_a_source_the_app_sets_itself_or_does_not_know_is_refused(api, auth_headers, source):
    response = post(api, auth_headers, source=source)

    assert response.status_code == 422
    assert httpx2.get(f"{api}/runs").json()["runs"] == []
