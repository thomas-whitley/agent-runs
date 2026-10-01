"""A fake of the GitHub REST calls Mercury makes, on a real local socket:
list the open pull requests for a head branch and open one (a repo chore),
and read a repo's default branch and its completed workflow runs (the CI
watch). It records every call and the Authorization header each carried.
Point GITHUB_API_URL at `url` to use it.
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
        # owner/name to default branch. A repo not listed answers 404.
        self.repos: dict[str, str] = {}
        # Each has repo, name, head_branch, conclusion, and optionally id.
        self.workflow_runs: list[dict] = []
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
            if self.status_override is not None:
                return self.status_override, {"message": "overridden"}
            parts = urlsplit(path)
            segments = parts.path.strip("/").split("/")
            if method == "GET" and segments[0] == "repos" and len(segments) in (3, 5):
                return self._read_repo(segments, parse_qs(parts.query))
            # /repos/{owner}/{name}/pulls
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

    def _read_repo(self, segments: list[str], query: dict) -> tuple[int, object]:
        repo = f"{segments[1]}/{segments[2]}"
        if repo not in self.repos:
            return 404, {"message": "Not Found"}
        if len(segments) == 3:
            return 200, {"full_name": repo, "default_branch": self.repos[repo]}
        if segments[3:] != ["actions", "runs"]:
            return 404, {"message": "Not Found"}
        branch = query.get("branch", [None])[0]
        # Newest first, as GitHub lists them. Only completed runs are listed,
        # because the watch asks for status=completed.
        runs = [
            {
                "id": run.get("id", 1000 + index),
                "name": run["name"],
                "head_branch": run["head_branch"],
                "head_sha": run.get("head_sha", "abcdef1234567"),
                "status": "completed",
                "conclusion": run["conclusion"],
                "html_url": f"https://github.com/{repo}/actions/runs/{run.get('id', 1000 + index)}",
            }
            for index, run in enumerate(self.workflow_runs)
            if run["repo"] == repo and (branch is None or run["head_branch"] == branch)
        ]
        return 200, {"total_count": len(runs), "workflow_runs": list(reversed(runs))}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
