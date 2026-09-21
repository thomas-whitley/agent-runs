"""The claim this repo exists to prove: drop a live stream, reconnect, resume."""

import httpx2
import psycopg
from psycopg.types.json import Jsonb

from tests.sse import read_sse


def append_event(database_url: str, run_id: str, seq: int, payload: dict) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO events (run_id, seq, payload) VALUES (%s, %s, %s)",
            (run_id, seq, Jsonb(payload)),
        )


def create_run(base_url: str) -> str:
    response = httpx2.post(f"{base_url}/runs", json={"task": "make the test pass"})
    response.raise_for_status()
    return response.json()["id"]


def seed_run(base_url: str, database_url: str, step_count: int) -> str:
    run_id = create_run(base_url)
    for seq in range(1, step_count + 1):
        append_event(database_url, run_id, seq, {"kind": "step", "seq": seq})
    append_event(database_url, run_id, step_count + 1, {"kind": "done", "status": "succeeded"})
    return run_id


def test_stream_replays_every_event_then_closes_on_done(start_server, clean_db):
    base_url = start_server()
    run_id = seed_run(base_url, clean_db, step_count=4)

    with httpx2.stream("GET", f"{base_url}/runs/{run_id}/events", timeout=15) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = list(read_sse(response.iter_lines()))

    assert [event.data["seq"] for event in events[:4]] == [1, 2, 3, 4]
    assert events[-1].event == "done"
    assert [event.id for event in events] == sorted(event.id for event in events)


def test_dropping_a_live_stream_and_reconnecting_resumes_exactly_once(start_server, clean_db):
    """Steps arrive while the client is connected. It drops after step 3, then resumes."""
    base_url = start_server(keepalive_seconds=0.2)
    run_id = create_run(base_url)

    first_pass = []
    with httpx2.stream("GET", f"{base_url}/runs/{run_id}/events", timeout=15) as response:
        assert response.status_code == 200
        reader = read_sse(response.iter_lines())

        for seq in (1, 2, 3):
            append_event(clean_db, run_id, seq, {"kind": "step", "seq": seq})
            first_pass.append(next(reader))

        # The run is still going and the connection is still open. Drop it here.

    assert [event.data["seq"] for event in first_pass] == [1, 2, 3]
    last_seen = first_pass[-1].id

    # Steps 4 and 5 happen while nobody is listening.
    append_event(clean_db, run_id, 4, {"kind": "step", "seq": 4})
    append_event(clean_db, run_id, 5, {"kind": "step", "seq": 5})
    append_event(clean_db, run_id, 6, {"kind": "done", "status": "succeeded"})

    with httpx2.stream(
        "GET",
        f"{base_url}/runs/{run_id}/events",
        headers={"Last-Event-ID": str(last_seen)},
        timeout=15,
    ) as response:
        second_pass = list(read_sse(response.iter_lines()))

    assert all(event.id > last_seen for event in second_pass), "an event was replayed"
    assert [event.data["seq"] for event in second_pass[:2]] == [4, 5]
    assert second_pass[-1].event == "done"

    delivered = [event.id for event in first_pass] + [event.id for event in second_pass]
    assert len(delivered) == len(set(delivered)), "an event arrived twice"
    assert len(delivered) == 6, "an event was lost"


def test_unknown_run_is_not_found(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/runs/00000000-0000-0000-0000-000000000000/events")

    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"


def test_reconnecting_after_done_closes_instead_of_hanging(start_server, clean_db):
    """A cursor at the done event must end the stream, not poll Postgres forever.

    Left open it holds a Container Apps replica above zero and breaks scale to
    zero, which is the thing that makes this free.
    """
    import time

    base_url = start_server(keepalive_seconds=0.2)
    run_id = seed_run(base_url, clean_db, step_count=2)

    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = %s", (run_id,)
        )

    with httpx2.stream("GET", f"{base_url}/runs/{run_id}/events", timeout=15) as response:
        events = list(read_sse(response.iter_lines()))
    last_id = events[-1].id

    # Keepalives keep bytes flowing, so a read timeout never fires. The only way
    # to see the hang is a wall clock deadline.
    deadline = time.monotonic() + 5
    closed = False
    lines: list[str] = []

    with httpx2.stream(
        "GET",
        f"{base_url}/runs/{run_id}/events",
        headers={"Last-Event-ID": str(last_id)},
        timeout=10,
    ) as response:
        for line in response.iter_lines():
            lines.append(line)
            if time.monotonic() > deadline:
                break
        else:
            closed = True

    assert closed, "the stream stayed open after done, sending keepalives forever"
    assert all(event.id > last_id for event in read_sse(lines))
