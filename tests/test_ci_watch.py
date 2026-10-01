"""The hourly CI failure watch: one site_check of kind ci_watch per repo,
created through the API and closed by the scheduler, reading the newest
completed run of each workflow on the repo's default branch. A new failure
sends one Telegram message. Another inside 24 hours of that message waits
for the digest.
"""

import pytest

from app.github import GitHubClient
from app.schedule_state import is_suspended
from app.scheduler import watch_ci
from app.telegram import TelegramClient
from tests.github_fake import FakeGitHub

BEARER_TOKEN = "test-bearer-token"
GITHUB_TOKEN = "github-test-token"
REPO = "owner/service"
CHAT_ID = 42


@pytest.fixture
def github():
    server = FakeGitHub()
    server.repos[REPO] = "main"
    yield server
    server.close()


@pytest.fixture
def api(start_server, monkeypatch) -> str:
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    return start_server()


def _watch(conn, api, github, telegram=None):
    return watch_ci(
        conn,
        (REPO,),
        api,
        BEARER_TOKEN,
        GitHubClient(GITHUB_TOKEN, github.url),
        telegram=telegram,
        chat_id=CHAT_ID if telegram else None,
    )


def _finding(conn, run_id: str) -> dict:
    return conn.execute(
        "SELECT output FROM steps WHERE run_id = %s AND seq = 1", (run_id,)
    ).fetchone()[0]


def _client(fake_telegram) -> TelegramClient:
    return TelegramClient("123:abc", fake_telegram.url)


def test_a_green_repo_closes_a_ci_watch_run_with_nothing_failing(migrated_db, api, github):
    github.workflow_runs += [
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "success"},
        {"repo": REPO, "name": "Publish", "head_branch": "main", "conclusion": "success"},
    ]

    [run_id] = _watch(migrated_db, api, github)

    row = migrated_db.execute(
        "SELECT type, check_kind, task, status FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("site_check", "ci_watch", REPO, "succeeded")
    finding = _finding(migrated_db, run_id)
    assert (finding["branch"], finding["workflows"], finding["failing"]) == ("main", 2, [])
    done = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s AND seq = 2", (run_id,)
    ).fetchone()[0]
    assert done == {"kind": "done", "seq": 2, "output": {"status": "succeeded"}}


def test_the_newest_run_of_each_workflow_on_the_default_branch_decides(migrated_db, api, github):
    github.workflow_runs += [
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure"},
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "success"},
        {"repo": REPO, "name": "CI", "head_branch": "feature", "conclusion": "failure"},
        {"repo": REPO, "name": "Publish", "head_branch": "main", "conclusion": "cancelled"},
        {
            "repo": REPO,
            "name": "Deploy",
            "head_branch": "main",
            "conclusion": "timed_out",
            "id": 77,
        },
    ]

    [run_id] = _watch(migrated_db, api, github)

    failing = _finding(migrated_db, run_id)["failing"]
    assert [(f["workflow"], f["conclusion"], f["id"]) for f in failing] == [
        ("Deploy", "timed_out", 77)
    ]
    assert failing[0]["url"] == f"https://github.com/{REPO}/actions/runs/77"


def test_the_token_goes_to_github_as_a_header(migrated_db, api, github):
    _watch(migrated_db, api, github)

    assert github.authorizations
    assert set(github.authorizations) == {f"Bearer {GITHUB_TOKEN}"}


def test_a_new_failure_sends_one_message_and_says_so(migrated_db, api, github, fake_telegram):
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 5}
    )

    [run_id] = _watch(migrated_db, api, github, _client(fake_telegram))

    [message] = fake_telegram.sent()
    assert message["chat_id"] == CHAT_ID
    assert REPO in message["text"] and "CI" in message["text"]
    assert f"https://github.com/{REPO}/actions/runs/5" in message["text"]
    assert _finding(migrated_db, run_id)["notified"] is True


def test_the_same_failure_an_hour_later_sends_nothing(migrated_db, api, github, fake_telegram):
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 5}
    )
    _watch(migrated_db, api, github, _client(fake_telegram))

    [second] = _watch(migrated_db, api, github, _client(fake_telegram))

    assert len(fake_telegram.sent()) == 1
    assert _finding(migrated_db, second)["notified"] is False
    assert len(_finding(migrated_db, second)["failing"]) == 1


def test_a_new_failure_inside_24_hours_of_a_message_waits_for_the_digest(
    migrated_db, api, github, fake_telegram
):
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 5}
    )
    _watch(migrated_db, api, github, _client(fake_telegram))
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 6}
    )

    [second] = _watch(migrated_db, api, github, _client(fake_telegram))

    assert len(fake_telegram.sent()) == 1
    assert _finding(migrated_db, second)["notified"] is False


def test_a_new_failure_a_day_after_the_last_message_sends_another(
    migrated_db, api, github, fake_telegram
):
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 5}
    )
    [first] = _watch(migrated_db, api, github, _client(fake_telegram))
    migrated_db.execute(
        "UPDATE runs SET created_at = now() - interval '25 hours' WHERE id = %s", (first,)
    )
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure", "id": 6}
    )

    _watch(migrated_db, api, github, _client(fake_telegram))

    assert len(fake_telegram.sent()) == 2


def test_a_github_error_fails_the_run_and_the_third_suspends_the_watch(
    migrated_db, api, github, fake_telegram
):
    github.status_override = 401

    runs = [_watch(migrated_db, api, github, _client(fake_telegram))[0] for _ in range(3)]

    statuses = migrated_db.execute(
        "SELECT status FROM runs WHERE id = ANY(%s::uuid[])", (runs,)
    ).fetchall()
    assert statuses == [("failed",)] * 3
    assert "HTTP 401" in _finding(migrated_db, runs[0])["error"]
    assert GITHUB_TOKEN not in _finding(migrated_db, runs[0])["error"]
    assert is_suspended(migrated_db, f"ci_watch:{REPO}")
    [message] = fake_telegram.sent()
    assert REPO in message["text"] and "suspended" in message["text"]

    assert _watch(migrated_db, api, github, _client(fake_telegram)) == []


def test_a_red_ci_does_not_count_towards_suspending_the_watch(migrated_db, api, github):
    github.workflow_runs.append(
        {"repo": REPO, "name": "CI", "head_branch": "main", "conclusion": "failure"}
    )

    for _ in range(3):
        _watch(migrated_db, api, github)

    assert not is_suspended(migrated_db, f"ci_watch:{REPO}")
