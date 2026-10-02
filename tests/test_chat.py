"""Free text from the allowed chat: the webhook turns it into a chat run, and
the worker asks the model, with the task registry as the schema, to pick a
task type and fill its inputs or ask one clarifying question."""

import json

import httpx2
import pytest
import yaml

from app.chat import MAX_TURNS, SYSTEM, run_chat
from app.model import StubModel
from app.telegram import TelegramClient

SECRET = "webhook-secret"
CHAT = 42
TEST_FILE = "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def bot(start_server, fake_telegram, monkeypatch, tmp_path):
    config = tmp_path / "mercury.yaml"
    config.write_text(yaml.dump({"telegram": {"chat_id": CHAT}}))
    monkeypatch.setenv("MERCURY_CONFIG_PATH", str(config))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    return start_server()


def say(base_url: str, text: str) -> None:
    body = {"update_id": 1, "message": {"message_id": 5, "chat": {"id": CHAT}, "text": text}}
    response = httpx2.post(
        f"{base_url}/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert response.status_code == 200


def chat_run(conn) -> str:
    return str(conn.execute("SELECT id FROM runs WHERE type = 'chat'").fetchone()[0])


def run_it(conn, fake_telegram, run_id: str, *replies: str) -> StubModel:
    model = StubModel(replies=list(replies), tokens_per_reply=321)
    client = TelegramClient("123:abc", base_url=fake_telegram.url)
    run_chat(conn, run_id, model, client)
    return model


def edits(fake_telegram) -> list[str]:
    return [payload["text"] for payload in fake_telegram.sent("editMessageText")]


def create(type_: str, **inputs) -> str:
    return json.dumps({"action": "create", "type": type_, "inputs": inputs})


def test_free_text_becomes_a_chat_run_with_one_placeholder_message(bot, fake_telegram, migrated_db):
    say(bot, "check the site for broken links")

    row = migrated_db.execute(
        "SELECT task, status, telegram_chat_id, telegram_message_id, source "
        "FROM runs WHERE type = 'chat'"
    ).fetchone()
    assert row == ("check the site for broken links", "pending", CHAT, 1, "telegram")
    assert [p["text"] for p in fake_telegram.sent()] == ["On it."]


def test_the_model_picks_pytest_and_the_run_is_created_on_the_same_message(
    bot, fake_telegram, migrated_db
):
    say(bot, "make this pass: ...")
    run_id = chat_run(migrated_db)

    run_it(migrated_db, fake_telegram, run_id, create("pytest", task=TEST_FILE))

    created = migrated_db.execute(
        "SELECT id, task, provider, telegram_chat_id, telegram_message_id, status "
        "FROM runs WHERE type = 'pytest'"
    ).fetchone()
    # Stripped, as POST /runs strips it.
    assert created[1:] == (TEST_FILE.strip(), "gemini", CHAT, 1, "pending")
    assert edits(fake_telegram) == [f"Started a pytest run, {str(created[0])[:8]}."]


def test_the_model_picks_a_lighthouse_check(bot, fake_telegram, migrated_db):
    say(bot, "run lighthouse on https://site.example/")

    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        create("site_check", task="https://site.example/", kind="lighthouse"),
    )

    assert migrated_db.execute(
        "SELECT task, check_kind FROM runs WHERE type = 'site_check'"
    ).fetchone() == ("https://site.example/", "lighthouse")


def test_the_model_asks_one_question_and_nothing_is_created(bot, fake_telegram, migrated_db):
    say(bot, "check my site")
    question = "Which URL, and uptime, Lighthouse or broken links?"

    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        json.dumps({"action": "ask", "question": question}),
    )

    assert migrated_db.execute("SELECT count(*) FROM runs WHERE type <> 'chat'").fetchone() == (0,)
    assert edits(fake_telegram) == [question]
    assert migrated_db.execute("SELECT role, text FROM telegram_turns ORDER BY id").fetchall() == [
        ("user", "check my site"),
        ("assistant", question),
    ]


def test_the_chat_run_closes_with_its_tokens_and_a_done_event(bot, fake_telegram, migrated_db):
    say(bot, "make this pass")
    run_id = chat_run(migrated_db)

    run_it(migrated_db, fake_telegram, run_id, create("pytest", task=TEST_FILE))

    status, tokens = migrated_db.execute(
        "SELECT status, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (status, tokens) == ("succeeded", 321)
    kinds = [
        row[0]["kind"]
        for row in migrated_db.execute(
            "SELECT payload FROM events WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()
    ]
    assert kinds == ["intent", "done"]


@pytest.mark.parametrize(
    "reply, expected",
    [
        (create("repo_chore", task="bump deps", repo="o/r"), "No repos are listed for chores"),
        (create("digest", task="today"), "The digest is not wired up yet."),
        (create("chat", task="hi"), "I could not work out a task from that."),
        (create("pytest", task="  "), "I could not work out a task from that."),
        (
            create("site_check", task="https://a.example", kind="ping"),
            "I could not work out a task from that.",
        ),
        ("not json at all", "I could not work out a task from that."),
        (json.dumps({"action": "dance"}), "I could not work out a task from that."),
    ],
)
def test_a_reply_that_cannot_become_a_run_says_so(bot, fake_telegram, migrated_db, reply, expected):
    say(bot, "do something")

    run_it(migrated_db, fake_telegram, chat_run(migrated_db), reply)

    assert migrated_db.execute("SELECT count(*) FROM runs WHERE type <> 'chat'").fetchone() == (0,)
    [edit] = edits(fake_telegram)
    assert edit.startswith(expected)


def test_a_fenced_json_reply_is_read(bot, fake_telegram, migrated_db):
    say(bot, "make this pass")

    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        "```json\n" + create("pytest", task=TEST_FILE) + "\n```",
    )

    assert migrated_db.execute("SELECT count(*) FROM runs WHERE type = 'pytest'").fetchone() == (1,)


def test_the_prompt_carries_the_registry_and_the_recent_turns(bot, fake_telegram, migrated_db):
    say(bot, "check my site")
    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        json.dumps({"action": "ask", "question": "Which URL?"}),
    )
    say(bot, "https://site.example/")
    second = str(
        migrated_db.execute(
            "SELECT id FROM runs WHERE type = 'chat' AND status = 'pending'"
        ).fetchone()[0]
    )

    model = run_it(
        migrated_db, fake_telegram, second, create("site_check", task="https://site.example/")
    )

    [prompt] = model.prompts
    assert prompt.index("check my site") < prompt.index("Which URL?")
    assert prompt.index("Which URL?") < prompt.index("https://site.example/")
    # The registry is the system prompt, which the stub does not record.
    assert all(name in SYSTEM for name in ("pytest", "site_check", "broken_links", "repo_chore"))


def test_memory_keeps_only_the_last_turns_per_chat(migrated_db, fake_telegram):
    for i in range(MAX_TURNS + 5):
        migrated_db.execute(
            "INSERT INTO telegram_turns (chat_id, role, text) VALUES (%s, 'user', %s)",
            (CHAT, f"turn {i}"),
        )
    migrated_db.execute(
        "INSERT INTO telegram_turns (chat_id, role, text) VALUES (7, 'user', 'other chat')"
    )
    run_id = str(
        migrated_db.execute(
            "INSERT INTO runs (task, type, telegram_chat_id, telegram_message_id) "
            "VALUES ('latest', 'chat', %s, 1) RETURNING id",
            (CHAT,),
        ).fetchone()[0]
    )

    model = run_it(
        migrated_db, fake_telegram, run_id, json.dumps({"action": "ask", "question": "?"})
    )

    assert "turn 4\n" not in model.prompts[0] and "turn 6" in model.prompts[0]
    assert "other chat" not in model.prompts[0]
    kept = migrated_db.execute(
        "SELECT count(*) FROM telegram_turns WHERE chat_id = %s", (CHAT,)
    ).fetchone()[0]
    assert kept == MAX_TURNS
    assert migrated_db.execute(
        "SELECT count(*) FROM telegram_turns WHERE chat_id = 7"
    ).fetchone() == (1,)


class FlakyModel:
    """Fails the way Gemini did on 2026-09-28 (503, high demand), then answers."""

    def __init__(self, failures: int, reply: str) -> None:
        self.failures = failures
        self.reply = reply
        self.calls = 0

    def complete(self, system: str, prompt: str):
        from app.model import ModelReply

        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("Error code: 503 - This model is currently experiencing high demand")
        return ModelReply(text=self.reply, tokens=50)


def test_a_model_that_fails_then_answers_is_retried(bot, fake_telegram, migrated_db):
    say(bot, "check my site")
    model = FlakyModel(failures=2, reply=json.dumps({"action": "ask", "question": "Which URL?"}))
    client = TelegramClient("123:abc", base_url=fake_telegram.url)

    run_chat(migrated_db, chat_run(migrated_db), model, client, retry_backoff_seconds=0)

    assert model.calls == 3
    assert edits(fake_telegram) == ["Which URL?"]


def test_a_model_that_never_answers_closes_the_run_and_says_so(bot, fake_telegram, migrated_db):
    say(bot, "check my site")
    run_id = chat_run(migrated_db)
    model = FlakyModel(failures=99, reply="")
    client = TelegramClient("123:abc", base_url=fake_telegram.url)

    run_chat(migrated_db, run_id, model, client, retry_backoff_seconds=0)

    status, finished = migrated_db.execute(
        "SELECT status, finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (status, finished) == ("error", True)
    [event] = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s", (run_id,)
    ).fetchall()
    assert event[0]["kind"] == "done" and event[0]["output"]["status"] == "error"
    assert edits(fake_telegram) == [
        "The model is not answering right now. Try again in a few minutes."
    ]
    assert migrated_db.execute("SELECT count(*) FROM telegram_turns").fetchone() == (0,)


def test_an_uptime_check_from_chat_is_run_at_once_and_answered_with_its_result(
    bot, fake_telegram, migrated_db
):
    """No worker claims an uptime check; the scheduler runs its own. One a
    chat creates is run by the chat run itself, or it would wait forever."""
    say(bot, f"is {bot}/health up?")
    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        create("site_check", task=f"{bot}/health"),
    )

    status, check_kind = migrated_db.execute(
        "SELECT status, check_kind FROM runs WHERE type = 'site_check'"
    ).fetchone()
    assert (status, check_kind) == ("succeeded", "uptime")
    kinds = [
        row[0]
        for row in migrated_db.execute(
            "SELECT kind FROM steps s JOIN runs r ON r.id = s.run_id "
            "WHERE r.type = 'site_check' ORDER BY seq"
        )
    ]
    assert kinds == ["check", "done"]
    [reply] = edits(fake_telegram)
    assert f"{bot}/health is up" in reply and "200" in reply


def test_an_uptime_check_from_chat_that_fails_says_so(bot, fake_telegram, migrated_db):
    say(bot, "is http://127.0.0.1:1 up?")
    run_it(
        migrated_db,
        fake_telegram,
        chat_run(migrated_db),
        create("site_check", task="http://127.0.0.1:1"),
    )

    status = migrated_db.execute("SELECT status FROM runs WHERE type = 'site_check'").fetchone()
    assert status == ("failed",)
    [reply] = edits(fake_telegram)
    assert "http://127.0.0.1:1 is down" in reply


def test_a_run_the_chat_creates_is_sourced_from_telegram(bot, fake_telegram, migrated_db):
    say(bot, "make this pass: ...")

    run_it(migrated_db, fake_telegram, chat_run(migrated_db), create("pytest", task=TEST_FILE))

    assert migrated_db.execute("SELECT source FROM runs WHERE type = 'pytest'").fetchone() == (
        "telegram",
    )
