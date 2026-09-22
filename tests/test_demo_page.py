"""The demo page: static, no framework, EventSource plus a kill connection button."""

import httpx2


def test_the_demo_page_is_served_at_root(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "EventSource" in response.text
    assert "kill" in response.text.lower()


def test_the_demo_page_does_not_shadow_the_api_routes(start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
