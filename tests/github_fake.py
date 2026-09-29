"""A fake of the two GitHub REST calls a repo chore makes, on a real local
socket: list the open pull requests for a head branch, and open one. It
records every call and the Authorization header each carried. Point
GITHUB_API_URL at `url` to use it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.authorizations: list[str] = []
        self.pulls: list[dict] = []
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

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                status, body = fake._answer("GET", self.path, {}, self.headers)
                self._reply(status, body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                status, body = fake._answer("POST", self.path, payload, self.headers)
                self._reply(status, body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _answer(self, method: str, path: str, payload: dict, headers) -> tuple[int, object]:
        with self._lock:
            self.calls.append((method, path, payload))
            self.authorizations.append(headers.get("Authorization", ""))
            parts = urlsplit(path)
            # /repos/{owner}/{name}/pulls
            segments = parts.path.strip("/").split("/")
            if len(segments) != 4 or segments[0] != "repos" or segments[3] != "pulls":
                return 404, {"message": "Not Found"}
            repo = f"{segments[1]}/{segments[2]}"
            if method == "GET":
                head = parse_qs(parts.query).get("head", [""])[0]
                owner = segments[1]
                return 200, [
                    p for p in self.pulls if p["repo"] == repo and f"{owner}:{p['head']}" == head
                ]
            number = len(self.pulls) + 1
            pull = {
                "repo": repo,
                "number": number,
                "head": payload["head"],
                "base": payload["base"],
                "title": payload["title"],
                "body": payload.get("body", ""),
                "html_url": f"https://github.com/{repo}/pull/{number}",
            }
            self.pulls.append(pull)
            return 201, pull

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
