"""A run's events with no token: every step's id, number and kind, and the
done event's status, with every body left out. The runs page streams this,
so the kill connection button works in a browser that holds no token.

The bodies (generated code, test output, diffs) stay behind the bearer
token. A token that is present and wrong is still refused, rather than
quietly answered as if it were absent. A browser EventSource cannot set
Last-Event-ID on a fresh connection, so ?after= carries the same cursor.
"""

import httpx2

from tests.sse import read_sse
from tests.test_resume import append_event, create_run

CODE = "def roman(n):\n    return 'IV'\n"


def seed(base_url: str, database_url: str, headers: dict[str, str]) -> str:
    run_id = create_run(base_url, headers)
    append_event(database_url, run_id, 1, {"seq": 1, "kind": "plan", "output": {"text": "x"}})
    append_event(database_url, run_id, 2, {"seq": 2, "kind": "act", "output": {"code": CODE}})
    append_event(
        database_url,
        run_id,
        3,
        {"seq": 3, "kind": "done", "output": {"status": "succeeded", "attempts": 1}},
    )
    return run_id


def read(base_url: str, run_id: str, **kwargs) -> list:
    with httpx2.stream("GET", f"{base_url}/runs/{run_id}/events", timeout=15, **kwargs) as r:
        assert r.status_code == 200
        return list(read_sse(r.iter_lines()))


def test_without_a_token_each_step_arrives_with_its_kind_and_no_body(
    start_server, clean_db, auth_headers
):
    base_url = start_server()
    run_id = seed(base_url, clean_db, auth_headers)

    events = read(base_url, run_id)

    assert [e.data for e in events] == [
        {"seq": 1, "kind": "plan"},
        {"seq": 2, "kind": "act"},
        {"seq": 3, "kind": "done", "output": {"status": "succeeded"}},
    ]
    assert [e.event for e in events] == ["step", "step", "done"]


def test_with_the_token_the_bodies_are_there(start_server, clean_db, auth_headers):
    base_url = start_server()
    run_id = seed(base_url, clean_db, auth_headers)

    events = read(base_url, run_id, headers=auth_headers)

    assert events[1].data["output"]["code"] == CODE


def test_a_wrong_token_is_refused_not_downgraded(start_server, clean_db, auth_headers):
    base_url = start_server()
    run_id = seed(base_url, clean_db, auth_headers)

    response = httpx2.get(
        f"{base_url}/runs/{run_id}/events", headers={"Authorization": "Bearer wrong"}
    )

    assert response.status_code == 401


def test_after_resumes_like_last_event_id(start_server, clean_db, auth_headers):
    base_url = start_server()
    run_id = seed(base_url, clean_db, auth_headers)
    first = read(base_url, run_id)

    resumed = read(base_url, run_id, params={"after": first[0].id})

    assert [e.id for e in resumed] == [e.id for e in first[1:]]


def test_the_header_wins_over_after(start_server, clean_db, auth_headers):
    base_url = start_server()
    run_id = seed(base_url, clean_db, auth_headers)
    first = read(base_url, run_id)

    resumed = read(
        base_url,
        run_id,
        params={"after": first[0].id},
        headers={"Last-Event-ID": str(first[1].id)},
    )

    assert [e.id for e in resumed] == [first[2].id]
