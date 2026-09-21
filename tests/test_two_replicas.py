"""Two replicas serve one run, and a reconnect landing on the other resumes.

This one needs the compose stack up, because the proof is two separate
processes behind one port. Run it with:

    docker compose up --build -d --wait
    AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest -m integration
"""

import http.client
import json
import os
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

MAX_RECONNECT_TRIES = 12


@pytest.fixture(scope="module")
def base_url() -> str:
    url = os.environ.get("AGENT_RUNS_BASE_URL")
    if not url:
        pytest.skip("set AGENT_RUNS_BASE_URL to run against a live compose stack")
    return url.rstrip("/")


@pytest.fixture(scope="module")
def live_database_url() -> str:
    """The stack's own database. Nothing here drops or recreates a schema."""
    return os.environ.get("TEST_DATABASE_URL", "postgresql://agent:agent@localhost:5432/agent_runs")


def _connect(base_url: str) -> http.client.HTTPConnection:
    parts = urlsplit(base_url)
    return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=60)


def create_run(base_url: str) -> str:
    conn = _connect(base_url)
    conn.request(
        "POST",
        "/runs",
        json.dumps({"task": "two replica check"}),
        {"content-type": "application/json"},
    )
    body = json.load(conn.getresponse())
    conn.close()
    return body["id"]


def append_event(database_url: str, run_id: str, seq: int, payload: dict) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO events (run_id, seq, payload) VALUES (%s, %s, %s) "
            "ON CONFLICT (run_id, seq) DO NOTHING",
            (run_id, seq, Jsonb(payload)),
        )


def read_stream(base_url: str, run_id: str, last_event_id: int | None, stop_after: int | None):
    """Return (replica, events). Closes the socket early when stop_after is set."""
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)

    conn = _connect(base_url)
    conn.request("GET", f"/runs/{run_id}/events", headers=headers)
    response = conn.getresponse()
    assert response.status == 200
    replica = response.getheader("X-Replica")

    events: list[tuple[int, dict]] = []
    fields: dict[str, str] = {}
    buffer = b""

    while True:
        chunk = response.read(1)
        if not chunk:
            break
        buffer += chunk
        if not buffer.endswith(b"\n"):
            continue
        line = buffer.decode().rstrip("\n")
        buffer = b""

        if line.startswith(":"):
            continue
        if line != "":
            name, _, value = line.partition(":")
            fields[name] = value.lstrip()
            continue
        if not fields:
            continue

        events.append((int(fields["id"]), json.loads(fields["data"])))
        fields = {}
        if stop_after is not None and len(events) >= stop_after:
            break

    conn.close()
    return replica, events


def test_a_stream_dropped_on_one_replica_resumes_on_the_other(base_url, live_database_url):
    run_id = create_run(base_url)
    for seq in (1, 2, 3):
        append_event(live_database_url, run_id, seq, {"kind": "step", "seq": seq})

    first_replica, first_pass = read_stream(base_url, run_id, None, stop_after=3)
    assert [event[1]["seq"] for event in first_pass] == [1, 2, 3]
    last_seen = first_pass[-1][0]

    # The run carries on while nobody is connected.
    for seq in (4, 5):
        append_event(live_database_url, run_id, seq, {"kind": "step", "seq": seq})
    append_event(
        live_database_url, run_id, 6, {"kind": "done", "seq": 6, "output": {"status": "succeeded"}}
    )

    # Replaying from a cursor is idempotent, so retrying until the proxy sends us
    # to the other replica costs nothing and proves the point.
    replicas_seen = {first_replica}
    second_pass = None
    for _ in range(MAX_RECONNECT_TRIES):
        replica, events = read_stream(base_url, run_id, last_seen, stop_after=None)
        replicas_seen.add(replica)
        second_pass = events
        if replica != first_replica:
            break
    else:
        pytest.fail(f"never landed on a different replica, saw only {replicas_seen}")

    assert len(replicas_seen) >= 2, "the two passes were served by one process"
    assert all(event[0] > last_seen for event in second_pass), "an event was replayed"
    assert [event[1]["seq"] for event in second_pass] == [4, 5, 6]
    assert second_pass[-1][1]["kind"] == "done"

    delivered = [event[0] for event in first_pass] + [event[0] for event in second_pass]
    assert len(delivered) == len(set(delivered)), "an event arrived twice"
    assert len(delivered) == 6, "an event was lost"


def test_both_replicas_answer_through_the_one_port(base_url):
    replicas = set()
    for _ in range(20):
        conn = _connect(base_url)
        conn.request("GET", "/health")
        response = conn.getresponse()
        response.read()
        replicas.add(response.getheader("X-Replica"))
        conn.close()

    assert len(replicas) >= 2, f"only one replica answered: {replicas}"
