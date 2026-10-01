"""The dependency audit. It reads the pinned versions from a repo's uv.lock
and package-lock.json files and asks OSV.dev which carry a known advisory.
OSV merges the PyPA and GitHub advisory databases, the data pip-audit and npm
audit read, so the image needs neither tool and no Node. Only lockfile
versions 2 and 3 of package-lock.json are read, which npm 7 and later write.
"""

import json
import tomllib
import urllib.error
import urllib.request
from pathlib import PurePosixPath

from app.github import GitHubClient

LOCK_FILES = {"uv.lock": "PyPI", "package-lock.json": "npm"}
OSV_TIMEOUT_SECONDS = 30.0
# OSV takes at most 1,000 queries in one batch.
OSV_BATCH = 1000
# Bounds what one audit stores: the vulnerable packages kept, and the
# advisories whose summary and severity are fetched.
MAX_VULNERABLE = 50
MAX_ADVISORIES = 20

Package = tuple[str, str, str]  # ecosystem, name, version


class OSVError(Exception):
    """OSV refused or could not be reached."""


class OSVClient:
    def __init__(self, api_url: str = "https://api.osv.dev") -> None:
        self._api_url = api_url.rstrip("/")

    def affected(self, packages: list[Package]) -> list[list[str]]:
        """For each package, in order, the ids of the advisories that affect it."""
        ids: list[list[str]] = []
        for start in range(0, len(packages), OSV_BATCH):
            queries = [
                {"package": {"ecosystem": ecosystem, "name": name}, "version": version}
                for ecosystem, name, version in packages[start : start + OSV_BATCH]
            ]
            results = self._call("POST", "/v1/querybatch", {"queries": queries})["results"]
            ids += [[vuln["id"] for vuln in result.get("vulns", [])] for result in results]
        return ids

    def advisory(self, advisory_id: str) -> dict:
        record = self._call("GET", f"/v1/vulns/{advisory_id}")
        return {
            "summary": record.get("summary"),
            "severity": (record.get("database_specific") or {}).get("severity"),
            "aliases": record.get("aliases", []),
        }

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "User-Agent": "mercury"},
        )
        try:
            with urllib.request.urlopen(request, timeout=OSV_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise OSVError(f"OSV {method} {path}: HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise OSVError(f"OSV {method}: {type(error).__name__}") from None


def lock_file_packages(path: str, text: str) -> list[Package]:
    """The packages a lock file pins, in file order. The project itself and
    anything installed from a local path are left out, since no advisory
    database knows them."""
    name = PurePosixPath(path).name
    if name == "uv.lock":
        return [
            ("PyPI", package["name"], package["version"])
            for package in tomllib.loads(text).get("package", [])
            if "registry" in package.get("source", {})
        ]
    if name == "package-lock.json":
        packages = json.loads(text).get("packages", {})
        return [
            ("npm", key.rsplit("node_modules/", 1)[1], entry["version"])
            for key, entry in packages.items()
            if key and not entry.get("link") and "version" in entry
        ]
    raise ValueError(f"not a lock file the audit reads: {path}")


def audit_repo(github: GitHubClient, osv: OSVClient, repo: str) -> dict:
    """The finding for one repo: each lock file read, each vulnerable package
    with its advisory ids, and the summary and severity of the advisories."""
    branch = github.default_branch(repo)
    paths = [
        path
        for path in github.file_paths(repo, branch)
        if PurePosixPath(path).name in LOCK_FILES and "node_modules/" not in path
    ]

    lock_files: list[dict] = []
    pinned: list[tuple[Package, str]] = []
    for path in sorted(paths):
        packages = lock_file_packages(path, github.raw_file(repo, path, branch))
        lock_files.append(
            {
                "path": path,
                "ecosystem": LOCK_FILES[PurePosixPath(path).name],
                "packages": len(packages),
            }
        )
        pinned += [(package, path) for package in packages]

    affected = osv.affected([package for package, _ in pinned]) if pinned else []
    vulnerable = [
        {"ecosystem": ecosystem, "package": name, "version": version, "lock_file": path, "ids": ids}
        for ((ecosystem, name, version), path), ids in zip(pinned, affected, strict=True)
        if ids
    ]

    advisory_ids = list(dict.fromkeys(i for entry in vulnerable for i in entry["ids"]))
    advisories = {i: osv.advisory(i) for i in advisory_ids[:MAX_ADVISORIES]}
    return {
        "repo": repo,
        "branch": branch,
        "lock_files": lock_files,
        "vulnerable": vulnerable[:MAX_VULNERABLE],
        "vulnerable_count": len(vulnerable),
        "advisories": advisories,
        "advisory_count": len(advisory_ids),
    }
