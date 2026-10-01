"""A fake of the two OSV.dev calls the dependency audit makes, on a real
local socket: POST /v1/querybatch and GET /v1/vulns/{id}. Point OSV_API_URL
at `url` to use it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOSV:
    def __init__(self) -> None:
        # (ecosystem, name, version) to the advisory ids that affect it.
        self.affected: dict[tuple[str, str, str], list[str]] = {}
        # advisory id to the record GET /v1/vulns/{id} returns.
        self.advisories: dict[str, dict] = {}
        self.queries: list[dict] = []
        self.status_override: int | None = None
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._reply(*fake._batch(self.path, body))

            def do_GET(self) -> None:  # noqa: N802
                self._reply(*fake._advisory(self.path))

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _batch(self, path: str, body: dict) -> tuple[int, object]:
        with self._lock:
            if self.status_override is not None:
                return self.status_override, {"message": "overridden"}
            if path != "/v1/querybatch":
                return 404, {"message": "Not Found"}
            self.queries.extend(body["queries"])
            results = []
            for query in body["queries"]:
                key = (query["package"]["ecosystem"], query["package"]["name"], query["version"])
                ids = self.affected.get(key, [])
                results.append({"vulns": [{"id": i} for i in ids]} if ids else {})
            return 200, {"results": results}

    def _advisory(self, path: str) -> tuple[int, object]:
        with self._lock:
            advisory_id = path.removeprefix("/v1/vulns/")
            if advisory_id not in self.advisories:
                return 404, {"message": "Not Found"}
            return 200, {"id": advisory_id, **self.advisories[advisory_id]}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
