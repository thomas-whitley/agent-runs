"""A Telegram message opens a pull request on a named repo, against the real
thomas-whitley/mercury-fixture on GitHub.

The message goes into the webhook, the chat run turns it into a repo chore
waiting for approval, Approve is pressed, and the worker clones the fixture,
runs its own tests and opens the pull request. The model is the stub, so the
change is fixed and no model key is needed. Telegram is the fake. GitHub is
real, so this needs a token that can push to the fixture:

    export MERCURY_GITHUB_TOKEN=github_pat_...
    uv run pytest tests/test_repo_chore_github.py -m integration -o addopts= -v

The test closes its pull request and deletes its branch whatever happens.
"""

import json
import os

import httpx2
import pytest

from app.config import load_settings
from app.mercury_config import RepoConfig
from app.model import StubModel
from app.worker import claim_next_run, process_run
from tests.test_approvals import press
from tests.test_repo_chore_approval import bot, chore, run_it, say  # noqa: F401

pytestmark = pytest.mark.integration

FIXTURE = "thomas-whitley/mercury-fixture"
TEST_COMMAND = "python -m unittest -v"
REPOS = (RepoConfig(name=FIXTURE, test_command=TEST_COMMAND),)
INSTRUCTION = "Add subtract to calc.py, with a test"
WORKER = "worker-1"

CALC = '''"""A deliberately small module for Mercury's repo chore integration test to edit."""


def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b
'''
TEST_SUBTRACT = """import unittest

from calc import subtract


class SubtractTest(unittest.TestCase):
    def test_subtracts_the_second_from_the_first(self):
        self.assertEqual(subtract(5, 3), 2)
"""


@pytest.fixture
def github_token() -> str:
    token = os.environ.get("MERCURY_GITHUB_TOKEN")
    if not token:
        pytest.skip("export a MERCURY_GITHUB_TOKEN that can push to mercury-fixture")
    return token


@pytest.fixture
def github(github_token):
    """A client for the real API, and the cleanup of every run's branch and pull request."""
    client = httpx2.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "mercury-test",
        },
        timeout=20,
    )
    branches: list[str] = []
    yield client, branches
    for branch in branches:
        pulls = client.get(
            f"/repos/{FIXTURE}/pulls",
            params={"state": "open", "head": f"thomas-whitley:{branch}"},
        ).json()
        for pull in pulls:
            client.patch(f"/repos/{FIXTURE}/pulls/{pull['number']}", json={"state": "closed"})
        client.delete(f"/repos/{FIXTURE}/git/refs/heads/{branch}")
    client.close()


def test_a_telegram_message_opens_a_pull_request_on_the_fixture(
    bot,  # noqa: F811, the fixture imported from test_repo_chore_approval
    fake_telegram,
    migrated_db,
    clean_db,
    github,
    monkeypatch,
):
    client, branches = github
    say(bot, "add subtract to the fixture")
    run_it(migrated_db, fake_telegram, chore(repo=FIXTURE, task=INSTRUCTION), repos=REPOS)
    run_id, approval_id = migrated_db.execute(
        "SELECT r.id::text, a.id FROM runs r JOIN approvals a ON a.run_id = r.id "
        "WHERE r.type = 'repo_chore'"
    ).fetchone()
    branch = f"agent/{run_id}"
    branches.append(branch)

    press(bot, approval_id, "yes")
    assert str(claim_next_run(migrated_db, WORKER)) == run_id
    monkeypatch.setenv("DATABASE_URL", clean_db)
    monkeypatch.setenv("WORKER_ID", WORKER)
    model = StubModel(
        replies=[
            json.dumps({"read": ["calc.py", "test_calc.py"]}),
            json.dumps(
                {
                    "files": {"calc.py": CALC, "test_subtract.py": TEST_SUBTRACT},
                    "summary": "Add subtract",
                }
            ),
        ]
    )
    result = process_run(
        migrated_db,
        run_id,
        load_settings(),
        model_builder=lambda settings, provider: model,
        repos=REPOS,
    )

    assert result.status == "succeeded"
    done = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'done'", (run_id,)
    ).fetchone()[0]
    print(f"\nrun {run_id} opened {done['pr_url']}")
    number = int(done["pr_url"].rstrip("/").rsplit("/", 1)[1])
    pull = client.get(f"/repos/{FIXTURE}/pulls/{number}").json()
    assert pull["state"] == "open"
    assert pull["head"]["ref"] == branch
    assert pull["base"]["ref"] == "main"
    files = client.get(f"/repos/{FIXTURE}/pulls/{number}/files").json()
    assert sorted(f["filename"] for f in files) == ["calc.py", "test_subtract.py"]
