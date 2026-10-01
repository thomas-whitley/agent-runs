"""The GitHub REST calls Mercury makes, with the token as a header. A repo
chore lists and opens pull requests. The CI watch reads a repo's default
branch and its completed workflow runs.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import quote

GITHUB_TIMEOUT_SECONDS = 20.0


class GitHubError(Exception):
    """GitHub refused or could not be reached. The message never carries the token."""


class GitHubClient:
    def __init__(self, token: str | None, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def find_pull(self, repo: str, branch: str) -> str | None:
        owner = repo.split("/", 1)[0]
        pulls = self._call("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
        return pulls[0]["html_url"] if pulls else None

    def open_pull(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        pull = self._call(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return pull["html_url"]

    def default_branch(self, repo: str) -> str:
        return self._call("GET", f"/repos/{repo}")["default_branch"]

    def completed_workflow_runs(self, repo: str, branch: str, limit: int = 50) -> list[dict]:
        """The newest completed workflow runs on one branch, newest first."""
        path = (
            f"/repos/{repo}/actions/runs?branch={quote(branch)}&status=completed&per_page={limit}"
        )
        return self._call("GET", path)["workflow_runs"]

    def _call(self, method: str, path: str, payload: dict | None = None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "mercury"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise GitHubError(f"GitHub {method} {path.split('?')[0]}: HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise GitHubError(f"GitHub {method}: {type(error).__name__}") from None
