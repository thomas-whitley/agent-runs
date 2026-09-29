"""Approvals: a question the bot asks with inline buttons, one row per
question, answered once by a button press from the one allowed chat, and
expired after 24 hours when nobody answers.
"""

import httpx2
import pytest
import yaml

from app.approvals import ask, expire_due
from app.schedule_state import is_suspended, record_failure
from app.scheduler import run_due_checks
from app.telegram import TelegramClient
from tests.test_telegram_webhook import CHAT, SECRET, SITE, insert_run, post, update

SCHEDULE = f"site_uptime:{SITE}"


@pytest.fixture
def bot(start_server, fake_telegram, monkeypatch, tmp_path):
    config = tmp_path / "mercury.yaml"
    config.write_text(yaml.dump({"telegram": {"chat_id": CHAT}, "portfolio": {"sites": [SITE]}}))
    monkeypatch.setenv("MERCURY_CONFIG_PATH", str(config))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    return start_server()


def client(fake_telegram) -> TelegramClient:
    return TelegramClient("123:abc", fake_telegram.url)


def press(base_url: str, approval_id: int, choice: str, chat_id: int = CHAT) -> httpx2.Response:
    body = {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": chat_id},
            "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}},
            "data": f"approval:{approval_id}:{choice}",
        },
    }
    return httpx2.post(
        f"{base_url}/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )


def suspend(conn) -> None:
    for _ in range(3):
        record_failure(conn, SCHEDULE)


def approval(conn, approval_id: int) -> tuple:
    return conn.execute(
        "SELECT answer, answered_at IS NOT NULL FROM approvals WHERE id = %s", (approval_id,)
    ).fetchone()


def edits(fake_telegram) -> list[str]:
    return [payload["text"] for payload in fake_telegram.sent("editMessageText")]


def test_record_failure_says_when_it_suspends_and_only_then(migrated_db):
    assert record_failure(migrated_db, SCHEDULE) is False
    assert record_failure(migrated_db, SCHEDULE) is False
    assert record_failure(migrated_db, SCHEDULE) is True
    assert record_failure(migrated_db, SCHEDULE) is False


def test_ask_sends_one_message_with_buttons_carrying_the_row_id(migrated_db, fake_telegram):
    approval_id = ask(
        migrated_db,
        client(fake_telegram),
        CHAT,
        "resume_schedule",
        "Resume it?",
        schedule_name=SCHEDULE,
    )

    [message] = fake_telegram.sent()
    assert message["text"] == "Resume it?"
    buttons = [b for row in message["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["callback_data"] for b in buttons] == [f"approval:{approval_id}:yes"]
    row = migrated_db.execute(
        "SELECT message_id, expires_at - created_at FROM approvals WHERE id = %s", (approval_id,)
    ).fetchone()
    assert row[0] == 1
    assert row[1].total_seconds() == 24 * 3600


def test_a_start_run_approval_offers_approve_and_decline(migrated_db, fake_telegram):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")

    approval_id = ask(migrated_db, client(fake_telegram), CHAT, "start_run", "Go?", run_id=run_id)

    [message] = fake_telegram.sent()
    buttons = [b for row in message["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["callback_data"] for b in buttons] == [
        f"approval:{approval_id}:yes",
        f"approval:{approval_id}:no",
    ]


def test_pressing_resume_resumes_the_schedule(bot, fake_telegram, migrated_db):
    suspend(migrated_db)
    approval_id = ask(
        migrated_db,
        client(fake_telegram),
        CHAT,
        "resume_schedule",
        "Suspended.",
        schedule_name=SCHEDULE,
    )

    response = press(bot, approval_id, "yes")

    assert response.status_code == 200
    assert is_suspended(migrated_db, SCHEDULE) is False
    assert approval(migrated_db, approval_id) == ("approved", True)
    [edit] = fake_telegram.sent("editMessageText")
    assert edit["text"].startswith("Suspended.")
    assert "reply_markup" not in edit
    assert fake_telegram.sent("answerCallbackQuery")[0]["callback_query_id"] == "cb-1"


def test_approve_hands_a_waiting_run_to_the_worker(bot, fake_telegram, migrated_db):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    approval_id = ask(migrated_db, client(fake_telegram), CHAT, "start_run", "Go?", run_id=run_id)

    press(bot, approval_id, "yes")

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("pending",)
    assert approval(migrated_db, approval_id) == ("approved", True)


def test_decline_cancels_the_waiting_run_with_a_done_event(bot, fake_telegram, migrated_db):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    approval_id = ask(migrated_db, client(fake_telegram), CHAT, "start_run", "Go?", run_id=run_id)

    press(bot, approval_id, "no")

    row = migrated_db.execute(
        "SELECT status, finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("cancelled", True)
    [payload] = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert payload["kind"] == "done"
    assert payload["output"] == {"status": "cancelled"}
    assert approval(migrated_db, approval_id) == ("declined", True)


def test_a_second_press_changes_nothing(bot, fake_telegram, migrated_db):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    approval_id = ask(migrated_db, client(fake_telegram), CHAT, "start_run", "Go?", run_id=run_id)
    press(bot, approval_id, "yes")

    press(bot, approval_id, "no")

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("pending",)
    assert approval(migrated_db, approval_id) == ("approved", True)


def test_a_press_from_another_chat_does_nothing(bot, fake_telegram, migrated_db):
    suspend(migrated_db)
    approval_id = ask(
        migrated_db,
        client(fake_telegram),
        CHAT,
        "resume_schedule",
        "Suspended.",
        schedule_name=SCHEDULE,
    )

    response = press(bot, approval_id, "yes", chat_id=CHAT + 1)

    assert response.status_code == 200
    assert is_suspended(migrated_db, SCHEDULE) is True
    assert approval(migrated_db, approval_id) == (None, False)
    assert fake_telegram.sent("editMessageText") == []


def test_a_press_without_the_secret_is_refused(bot, fake_telegram, migrated_db):
    suspend(migrated_db)
    approval_id = ask(
        migrated_db,
        client(fake_telegram),
        CHAT,
        "resume_schedule",
        "Suspended.",
        schedule_name=SCHEDULE,
    )
    body = {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "message": {"message_id": 1, "chat": {"id": CHAT}},
            "data": f"approval:{approval_id}:yes",
        },
    }

    response = httpx2.post(f"{bot}/telegram", json=body)

    assert response.status_code == 401
    assert is_suspended(migrated_db, SCHEDULE) is True


def test_a_press_after_expiry_expires_the_approval_instead(bot, fake_telegram, migrated_db):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    approval_id = ask(migrated_db, client(fake_telegram), CHAT, "start_run", "Go?", run_id=run_id)
    migrated_db.execute(
        "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (approval_id,),
    )

    press(bot, approval_id, "yes")

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("cancelled",)
    assert approval(migrated_db, approval_id) == ("expired", True)
    assert "expired" in edits(fake_telegram)[0].lower()


def test_expire_due_cancels_a_waiting_run_and_edits_its_message(migrated_db, fake_telegram):
    run_id = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    telegram = client(fake_telegram)
    approval_id = ask(migrated_db, telegram, CHAT, "start_run", "Go?", run_id=run_id)
    fresh_run = insert_run(migrated_db, type_="repo_chore", status="awaiting_approval")
    fresh_id = ask(migrated_db, telegram, CHAT, "start_run", "Go too?", run_id=fresh_run)
    migrated_db.execute(
        "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (approval_id,),
    )

    assert expire_due(migrated_db, telegram) == 1

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("cancelled",)
    assert approval(migrated_db, approval_id) == ("expired", True)
    assert approval(migrated_db, fresh_id) == (None, False)
    [edit] = fake_telegram.sent("editMessageText")
    assert edit["text"].startswith("Go?")
    assert "expired" in edit["text"].lower()
    assert "reply_markup" not in edit


def test_expire_due_leaves_a_schedule_suspended(migrated_db, fake_telegram):
    suspend(migrated_db)
    telegram = client(fake_telegram)
    approval_id = ask(
        migrated_db, telegram, CHAT, "resume_schedule", "Suspended.", schedule_name=SCHEDULE
    )
    migrated_db.execute(
        "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (approval_id,),
    )

    expire_due(migrated_db, telegram)

    assert is_suspended(migrated_db, SCHEDULE) is True
    assert approval(migrated_db, approval_id) == ("expired", True)


def test_resume_command_resumes_a_suspended_site(bot, fake_telegram, migrated_db):
    suspend(migrated_db)

    post(bot, update(f"/resume {SITE}"))

    assert is_suspended(migrated_db, SCHEDULE) is False
    [reply] = [payload["text"] for payload in fake_telegram.sent()]
    assert SITE in reply


def test_resume_command_without_a_site_lists_what_is_suspended(bot, fake_telegram, migrated_db):
    suspend(migrated_db)

    post(bot, update("/resume"))

    [reply] = [payload["text"] for payload in fake_telegram.sent()]
    assert "Usage" in reply
    assert SITE in reply
    assert is_suspended(migrated_db, SCHEDULE) is True


def test_the_third_failure_sends_one_message_with_a_resume_button(
    start_server, migrated_db, fake_telegram, monkeypatch
):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "test-bearer-token")
    base_url = start_server()
    unreachable = "http://127.0.0.1:1"
    telegram = client(fake_telegram)

    for _ in range(4):
        run_due_checks(
            migrated_db,
            (unreachable,),
            base_url,
            "test-bearer-token",
            telegram=telegram,
            chat_id=CHAT,
        )

    [message] = fake_telegram.sent()
    assert unreachable in message["text"]
    [[button]] = message["reply_markup"]["inline_keyboard"]
    assert button["text"] == "Resume"
    row = migrated_db.execute("SELECT action, schedule_name FROM approvals").fetchone()
    assert row == ("resume_schedule", f"site_uptime:{unreachable}")
