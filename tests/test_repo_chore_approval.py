"""A repo chore starts only after a button press. The chat echoes the repo
and the instruction with Approve and Decline, and the run waits where no
worker will claim it. Only repos listed in mercury.yaml with a test command
can have a chore, and POST /runs cannot create one at all.
"""

import json

import httpx2
import pytest
import yaml

from app.chat import run_chat
from app.mercury_config import RepoConfig, load_mercury_config
from app.model import StubModel
from app.telegram import TelegramClient
from app.worker import claim_next_run

CHAT = 42
SECRET = "webhook-secret"
REPO = "thomas-whitley/mercury-fixture"
REPOS = (RepoConfig(name=REPO, test_command="uv run pytest"),)
INSTRUCTION = "Add a test for subtract"


@pytest.fixture
def bot(start_server, fake_telegram, monkeypatch, tmp_path):
    config = tmp_path / "mercury.yaml"
    config.write_text(yaml.dump({"telegram": {"chat_id": CHAT}}))
    monkeypatch.setenv("MERCURY_CONFIG_PATH", str(config))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "test-bearer-token")
    return start_server()


def say(base_url: str, text: str) -> None:
    body = {"update_id": 1, "message": {"message_id": 5, "chat": {"id": CHAT}, "text": text}}
    httpx2.post(
        f"{base_url}/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )


def chore(repo: str = REPO, task: str = INSTRUCTION) -> str:
    return json.dumps(
        {"action": "create", "type": "repo_chore", "inputs": {"task": task, "repo": repo}}
    )


def run_it(conn, fake_telegram, *replies: str, repos=REPOS) -> StubModel:
    run_id = str(conn.execute("SELECT id FROM runs WHERE type = 'chat'").fetchone()[0])
    model = StubModel(replies=list(replies))
    run_chat(conn, run_id, model, TelegramClient("123:abc", fake_telegram.url), repos=repos)
    return model


def test_a_repo_chore_waits_for_approval_with_the_repo_and_instruction_echoed(
    bot, fake_telegram, migrated_db
):
    say(bot, "add a subtract test to the fixture")

    run_it(migrated_db, fake_telegram, chore())

    row = migrated_db.execute(
        "SELECT status, task, repo, telegram_chat_id, telegram_message_id "
        "FROM runs WHERE type = 'repo_chore'"
    ).fetchone()
    # Message 1 is "On it.", message 2 the question. Progress lands on the question.
    _, question = fake_telegram.sent()
    assert row == ("awaiting_approval", INSTRUCTION, REPO, CHAT, 2)
    assert REPO in question["text"]
    assert INSTRUCTION in question["text"]
    labels = [b["text"] for r in question["reply_markup"]["inline_keyboard"] for b in r]
    assert labels == ["Approve", "Decline"]
    [edit] = fake_telegram.sent("editMessageText")
    assert edit["message_id"] == 1
    assert "approval" in edit["text"].lower()


def test_a_waiting_repo_chore_is_not_claimed(bot, fake_telegram, migrated_db):
    say(bot, "add a subtract test to the fixture")
    run_it(migrated_db, fake_telegram, chore())
    # The chat run itself is finished; only the chore is left.

    assert claim_next_run(migrated_db, "worker-1") is None


def test_a_repo_outside_the_portfolio_is_refused(bot, fake_telegram, migrated_db):
    say(bot, "tidy someone else's repo")

    run_it(migrated_db, fake_telegram, chore(repo="someone/else"))

    assert migrated_db.execute(
        "SELECT count(*) FROM runs WHERE type = 'repo_chore'"
    ).fetchone() == (0,)
    [edit] = fake_telegram.sent("editMessageText")
    assert REPO in edit["text"]
    assert fake_telegram.sent("sendMessage")[1:] == []


def test_a_repo_with_no_test_command_is_refused(bot, fake_telegram, migrated_db):
    say(bot, "tidy the fixture")

    run_it(migrated_db, fake_telegram, chore(), repos=(RepoConfig(name=REPO, test_command=None),))

    assert migrated_db.execute(
        "SELECT count(*) FROM runs WHERE type = 'repo_chore'"
    ).fetchone() == (0,)
    [edit] = fake_telegram.sent("editMessageText")
    assert "test_command" in edit["text"]


def test_the_prompt_names_the_repos_a_chore_may_touch(bot, fake_telegram, migrated_db):
    say(bot, "tidy the fixture")

    model = run_it(migrated_db, fake_telegram, json.dumps({"action": "ask", "question": "Which?"}))

    assert REPO in model.prompts[0]


def test_post_runs_cannot_create_a_repo_chore(bot):
    response = httpx2.post(
        f"{bot}/runs",
        json={"type": "repo_chore", "inputs": {"task": INSTRUCTION, "repo": REPO}},
        headers={"Authorization": "Bearer test-bearer-token"},
    )

    assert response.status_code == 422


def test_repos_are_read_with_their_test_command(tmp_path):
    config = tmp_path / "mercury.yaml"
    config.write_text(
        yaml.dump(
            {
                "portfolio": {
                    "repos": [
                        {"name": REPO, "test_command": "uv run pytest"},
                        "owner/bare",
                    ]
                }
            }
        )
    )

    assert load_mercury_config(config).repos == (
        RepoConfig(name=REPO, test_command="uv run pytest"),
        RepoConfig(name="owner/bare", test_command=None),
    )
