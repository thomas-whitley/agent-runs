import httpx2


def test_an_idle_stream_sends_keepalive_comments(start_server):
    """A run with no events yet must still produce traffic, or an idle proxy closes it."""
    base_url = start_server(keepalive_seconds=0.2)

    created = httpx2.post(
        f"{base_url}/runs",
        json={"type": "pytest", "inputs": {"task": "a run that has not started"}},
    )
    run_id = created.json()["id"]

    keepalives = 0
    # Without keepalives this read blocks until the timeout and the test fails.
    with httpx2.stream("GET", f"{base_url}/runs/{run_id}/events", timeout=10) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith(":"):
                keepalives += 1
                if keepalives >= 2:
                    break

    assert keepalives >= 2
