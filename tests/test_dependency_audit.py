"""The weekly dependency audit: the pinned versions in each repo's uv.lock and
package-lock.json files, read through the GitHub API, checked against
OSV.dev. Findings go in the digest only, so nothing is sent to Telegram.
"""

import json

import pytest

from app.audit import OSVClient, lock_file_packages
from app.github import GitHubClient
from app.schedule_state import is_suspended
from app.scheduler import audit_dependencies
from app.telegram import TelegramClient
from tests.github_fake import FakeGitHub
from tests.osv_fake import FakeOSV

BEARER_TOKEN = "test-bearer-token"
REPO = "owner/service"

UV_LOCK = """version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "service"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "jinja2"
version = "2.4.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "fastapi"
version = "0.118.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "local-helper"
version = "0.0.1"
source = { directory = "helper" }
"""

PACKAGE_LOCK = json.dumps(
    {
        "name": "web",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "web", "version": "0.0.0"},
            "node_modules/lodash": {"version": "4.17.15"},
            "node_modules/@scope/kit": {"version": "1.2.3", "dev": True},
            "node_modules/@scope/kit/node_modules/lodash": {"version": "4.17.21"},
            "node_modules/linked": {"resolved": "../linked", "link": True},
        },
    }
)


def test_uv_lock_gives_the_registry_packages_and_skips_the_project_itself():
    assert lock_file_packages("uv.lock", UV_LOCK) == [
        ("PyPI", "jinja2", "2.4.1"),
        ("PyPI", "fastapi", "0.118.0"),
    ]


def test_package_lock_gives_every_installed_package_including_nested_ones():
    assert lock_file_packages("web/package-lock.json", PACKAGE_LOCK) == [
        ("npm", "lodash", "4.17.15"),
        ("npm", "@scope/kit", "1.2.3"),
        ("npm", "lodash", "4.17.21"),
    ]


@pytest.fixture
def github():
    server = FakeGitHub()
    server.repos[REPO] = "main"
    server.files[REPO] = {
        "uv.lock": UV_LOCK,
        "web/package-lock.json": PACKAGE_LOCK,
        "web/node_modules/dep/package-lock.json": PACKAGE_LOCK,
        "README.md": "# service",
    }
    yield server
    server.close()


@pytest.fixture
def osv():
    server = FakeOSV()
    server.affected[("PyPI", "jinja2", "2.4.1")] = ["GHSA-aaaa"]
    server.affected[("npm", "lodash", "4.17.15")] = ["GHSA-bbbb", "GHSA-cccc"]
    server.advisories["GHSA-aaaa"] = {
        "summary": "Sandbox escape in Jinja2",
        "aliases": ["CVE-2019-10906"],
        "database_specific": {"severity": "HIGH"},
    }
    server.advisories["GHSA-bbbb"] = {"summary": "Command Injection in lodash"}
    server.advisories["GHSA-cccc"] = {"summary": "Prototype pollution in lodash"}
    yield server
    server.close()


@pytest.fixture
def api(start_server, monkeypatch) -> str:
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    return start_server()


def _audit(conn, api, github, osv, **kwargs):
    return audit_dependencies(
        conn,
        (REPO,),
        api,
        BEARER_TOKEN,
        GitHubClient("token", github.url),
        OSVClient(osv.url),
        **kwargs,
    )


def _finding(conn, run_id: str) -> dict:
    return conn.execute(
        "SELECT output FROM steps WHERE run_id = %s AND seq = 1", (run_id,)
    ).fetchone()[0]


def test_an_audit_records_each_vulnerable_package_with_its_advisories(
    migrated_db, api, github, osv
):
    [run_id] = _audit(migrated_db, api, github, osv)

    row = migrated_db.execute(
        "SELECT type, check_kind, task, status FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("site_check", "dependency_audit", REPO, "succeeded")
    finding = _finding(migrated_db, run_id)
    assert finding["lock_files"] == [
        {"path": "uv.lock", "ecosystem": "PyPI", "packages": 2},
        {"path": "web/package-lock.json", "ecosystem": "npm", "packages": 3},
    ]
    assert finding["vulnerable"] == [
        {
            "ecosystem": "PyPI",
            "package": "jinja2",
            "version": "2.4.1",
            "lock_file": "uv.lock",
            "ids": ["GHSA-aaaa"],
        },
        {
            "ecosystem": "npm",
            "package": "lodash",
            "version": "4.17.15",
            "lock_file": "web/package-lock.json",
            "ids": ["GHSA-bbbb", "GHSA-cccc"],
        },
    ]
    assert finding["advisories"]["GHSA-aaaa"] == {
        "summary": "Sandbox escape in Jinja2",
        "severity": "HIGH",
        "aliases": ["CVE-2019-10906"],
    }
    assert finding["advisories"]["GHSA-bbbb"]["severity"] is None


def test_a_lock_file_inside_node_modules_is_not_audited(migrated_db, api, github, osv):
    _audit(migrated_db, api, github, osv)

    assert len(osv.queries) == 5


def test_the_audit_is_weekly(migrated_db, api, github, osv):
    [first] = _audit(migrated_db, api, github, osv)

    assert _audit(migrated_db, api, github, osv) == []

    migrated_db.execute(
        "UPDATE runs SET created_at = now() - interval '8 days' WHERE id = %s", (first,)
    )
    assert len(_audit(migrated_db, api, github, osv)) == 1


def test_a_repo_with_no_lock_files_is_a_clean_audit(migrated_db, api, github, osv):
    github.files[REPO] = {"README.md": "# service"}

    [run_id] = _audit(migrated_db, api, github, osv)

    finding = _finding(migrated_db, run_id)
    assert (finding["lock_files"], finding["vulnerable"]) == ([], [])
    assert osv.queries == []


def test_findings_go_to_the_digest_and_send_no_message(
    migrated_db, api, github, osv, fake_telegram
):
    _audit(
        migrated_db,
        api,
        github,
        osv,
        telegram=TelegramClient("123:abc", fake_telegram.url),
        chat_id=42,
    )

    assert fake_telegram.sent() == []


def test_an_osv_error_fails_the_audit_and_counts_towards_suspending_it(
    migrated_db, api, github, osv
):
    osv.status_override = 503

    [run_id] = _audit(migrated_db, api, github, osv)

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("failed",)
    assert "HTTP 503" in _finding(migrated_db, run_id)["error"]
    for _ in range(2):
        migrated_db.execute("UPDATE runs SET created_at = now() - interval '8 days'")
        _audit(migrated_db, api, github, osv)
    assert is_suspended(migrated_db, f"dependency_audit:{REPO}")
