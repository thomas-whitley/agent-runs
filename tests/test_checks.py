"""The cloud side check: plain HTTP status and latency, no browser, no
model call. A bad result is data, never an exception — the caller always
gets a SiteCheckResult back.
"""

import http.server
import threading
import time

import pytest

from app.checks import check_site


@pytest.fixture
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                time.sleep(0.3)
            if self.path == "/broken":
                self.send_response(500)
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_check_site_passes_on_a_2xx_response(local_server):
    result = check_site(local_server)

    assert result.passed is True
    assert result.status_code == 200
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.error is None


def test_check_site_fails_on_a_5xx_response(local_server):
    result = check_site(f"{local_server}/broken")

    assert result.passed is False
    assert result.status_code == 500


def test_check_site_fails_and_does_not_raise_on_a_timeout(local_server):
    result = check_site(f"{local_server}/slow", timeout_seconds=0.05)

    assert result.passed is False
    assert result.status_code is None
    assert result.error


def test_check_site_fails_and_does_not_raise_on_an_unreachable_host():
    result = check_site("http://127.0.0.1:1", timeout_seconds=1.0)

    assert result.passed is False
    assert result.error
