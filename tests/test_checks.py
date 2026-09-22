"""The cloud side check: plain HTTP status and latency, no browser, no
model call. A bad result is data, never an exception: the caller always
gets a SiteCheckResult back.
"""

import http.server
import socket
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


@pytest.fixture
def malformed_server():
    """A TCP server that sends non-HTTP bytes to trigger http.client.HTTPException."""

    def accept_and_send_garbage():
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(1)
        port = server_socket.getsockname()[1]

        # Signal the port to the fixture via a thread-safe mechanism
        port_holder.append(port)

        while not stop_event.is_set():
            server_socket.settimeout(0.1)
            try:
                client_socket, _ = server_socket.accept()
                client_socket.send(b"this is not http\r\n\r\n")
                client_socket.close()
            except TimeoutError:
                continue

        server_socket.close()

    port_holder = []
    stop_event = threading.Event()
    thread = threading.Thread(target=accept_and_send_garbage, daemon=True)
    thread.start()

    # Wait for the port to be available
    while not port_holder:
        time.sleep(0.001)

    yield f"http://127.0.0.1:{port_holder[0]}"
    stop_event.set()
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


def test_check_site_fails_and_does_not_raise_on_malformed_http(malformed_server):
    result = check_site(malformed_server, timeout_seconds=1.0)

    assert result.passed is False
    assert result.status_code is None
    assert result.error
