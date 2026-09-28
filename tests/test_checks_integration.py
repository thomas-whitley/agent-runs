"""A weekly check runs on the self hosted worker, and falls back to the cloud
path when that worker is offline. Both halves run against the compose stack:

    export MERCURY_BEARER_TOKEN=$(openssl rand -hex 32) CHECK_CLAIM_WINDOW=20 \\
        PAGESPEED_URL=http://127.0.0.1:9/runPagespeed
    docker compose up --build -d --wait
    docker compose --profile checks build checks
    AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest -m integration

The self hosted half runs real Lighthouse in the checks container against the
runs page the stack itself serves. The offline half never starts the checks
worker, and waits for the Python worker to take the check once the window is
up. PAGESPEED_URL points at nothing, so the cloud path records an error as
its finding and closes the check, which is what a posted result does
whatever it says.
"""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
# The page as the checks container sees it, through the compose proxy.
PAGE = "http://proxy:8000/"


@pytest.fixture(scope="module")
def base_url() -> str:
    url = os.environ.get("AGENT_RUNS_BASE_URL")
    if not url:
        pytest.skip("set AGENT_RUNS_BASE_URL to run against a live compose stack")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def token() -> str:
    value = os.environ.get("MERCURY_BEARER_TOKEN")
    if not value:
        pytest.skip("export the MERCURY_BEARER_TOKEN the stack was started with")
    return value


@pytest.fixture(scope="module")
def window_seconds() -> float:
    return float(os.environ.get("CHECK_CLAIM_WINDOW", "20"))


@pytest.fixture
def db():
    url = os.environ.get(
        "AGENT_RUNS_DATABASE_URL", "postgresql://agent:agent@localhost:5432/agent_runs"
    )
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


def create_check(base_url: str, token: str, kind: str) -> str:
    request = urllib.request.Request(
        f"{base_url}/runs",
        data=json.dumps({"type": "site_check", "inputs": {"task": PAGE, "kind": kind}}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["id"]


def finished(db, run_id: str):
    return db.execute(
        "SELECT r.status, r.executor, r.claimed_by, s.output FROM runs r "
        "LEFT JOIN steps s ON s.run_id = r.id AND s.kind = 'check' "
        "WHERE r.id = %s AND r.finished_at IS NOT NULL",
        (run_id,),
    ).fetchone()


def run_checks_worker_once() -> None:
    subprocess.run(
        ["docker", "compose", "--profile", "checks", "run", "--rm", "checks"]
        + ["node", "dist/main.js", "--once"],
        cwd=REPO,
        check=True,
        timeout=300,
    )


def run_self_hosted(db, run_id: str):
    # --once claims the oldest pending check it may take, which is this one
    # unless an earlier run of the stack left others waiting.
    for _ in range(5):
        run_checks_worker_once()
        row = finished(db, run_id)
        if row is not None:
            return row
    pytest.fail(f"check {run_id} was not closed by the checks worker")


def test_a_lighthouse_check_runs_on_the_self_hosted_worker(base_url, token, db):
    run_id = create_check(base_url, token, "lighthouse")

    status, executor, claimed_by, output = run_self_hosted(db, run_id)

    assert (status, executor, claimed_by) == ("succeeded", "self_hosted", "compose-checks")
    assert "error" not in output, output
    assert 0 <= output["scores"]["performance"] <= 1
    assert output["lcp_ms"] > 0
    print(f"self hosted lighthouse check {run_id}: {json.dumps(output)}")


def test_a_broken_links_check_runs_on_the_self_hosted_worker(base_url, token, db):
    run_id = create_check(base_url, token, "broken_links")

    status, executor, _, output = run_self_hosted(db, run_id)

    assert (status, executor) == ("succeeded", "self_hosted")
    assert output["pages_checked"] >= 1
    assert output["broken"] == []
    print(f"self hosted broken_links check {run_id}: {json.dumps(output)}")


def test_a_lighthouse_check_falls_back_to_the_cloud_when_the_worker_is_offline(
    base_url, token, db, window_seconds
):
    run_id = create_check(base_url, token, "lighthouse")
    created = time.monotonic()

    # Still waiting for the self hosted worker halfway through the window.
    time.sleep(window_seconds / 2)
    assert finished(db, run_id) is None

    deadline = created + window_seconds + 60
    row = None
    while row is None and time.monotonic() < deadline:
        time.sleep(1)
        row = finished(db, run_id)
    waited = time.monotonic() - created

    assert row is not None, "the cloud path never took the check"
    status, executor, claimed_by, output = row
    assert (status, executor) == ("succeeded", "cloud")
    assert claimed_by != "compose-checks"
    assert waited >= window_seconds
    print(f"cloud fallback took check {run_id} after {waited:.1f}s: {json.dumps(output)}")


def test_a_broken_links_check_never_falls_back(base_url, token, db, window_seconds):
    run_id = create_check(base_url, token, "broken_links")

    time.sleep(window_seconds + 10)

    assert finished(db, run_id) is None
    # Leave nothing pending for the next run of the stack to trip over.
    run_self_hosted(db, run_id)
