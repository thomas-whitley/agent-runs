"""The runs page: FastAPI serves the built web/ bundle behind every API route."""

import httpx2
import pytest

INDEX = "<!doctype html><title>agent-runs</title><div id=root></div>"


@pytest.fixture
def web_dist(tmp_path, monkeypatch):
    """A stand in for web/dist, so these tests do not need a Node build."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX)
    (tmp_path / "assets" / "app.js").write_text("console.log('page')")
    monkeypatch.setenv("WEB_DIST_DIR", str(tmp_path))
    return tmp_path


def test_the_page_is_served_at_root(web_dist, start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX


def test_an_unknown_path_falls_through_to_the_page(web_dist, start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/runs-page")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX


def test_a_built_asset_is_served_as_itself(web_dist, start_server):
    base_url = start_server()

    response = httpx2.get(f"{base_url}/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log('page')"


def test_the_api_routes_are_never_shadowed_by_the_page(web_dist, start_server):
    base_url = start_server()

    health = httpx2.get(f"{base_url}/health")
    runs = httpx2.get(f"{base_url}/runs")
    missing_run = httpx2.get(f"{base_url}/runs/00000000-0000-0000-0000-000000000000/events")

    assert health.json() == {"status": "ok"}
    assert runs.headers["content-type"].startswith("application/json")
    assert runs.json() == {"runs": [], "next_cursor": None}
    assert missing_run.status_code == 404
    assert missing_run.json() == {"detail": "run not found"}


def test_a_path_outside_the_bundle_is_not_served(web_dist, start_server):
    (web_dist.parent / "secret.txt").write_text("not for the page")
    base_url = start_server()

    response = httpx2.get(f"{base_url}/..%2Fsecret.txt")

    assert "not for the page" not in response.text


def test_the_page_is_revalidated_on_every_load(web_dist, start_server):
    """A cached index.html names asset hashes a redeploy has deleted."""
    base_url = start_server()

    for path in ("/", "/runs-page"):
        response = httpx2.get(f"{base_url}{path}")
        assert response.headers["cache-control"] == "no-cache"
