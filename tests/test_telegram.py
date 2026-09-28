"""The outbound Telegram client, against the fake Bot API."""

import pytest

from app.telegram import TelegramClient, TelegramError
from tests.telegram_fake import FakeTelegram


@pytest.fixture
def fake():
    server = FakeTelegram()
    yield server
    server.close()


def test_send_message_returns_the_new_message_id(fake):
    client = TelegramClient("123:abc", base_url=fake.url)

    first = client.send_message(42, "hello")
    second = client.send_message(42, "again")

    assert (first, second) == (1, 2)
    assert fake.sent() == [{"chat_id": 42, "text": "hello"}, {"chat_id": 42, "text": "again"}]
    assert fake.tokens == ["123:abc", "123:abc"]


def test_edit_message_text_edits_in_place(fake):
    client = TelegramClient("123:abc", base_url=fake.url)

    client.edit_message_text(42, 7, "step 3 of 5")

    assert fake.sent("editMessageText") == [{"chat_id": 42, "message_id": 7, "text": "step 3 of 5"}]


def test_an_edit_that_changes_nothing_is_not_an_error(fake):
    client = TelegramClient("123:abc", base_url=fake.url)
    fake.fail_next = {
        "error_code": 400,
        "description": "Bad Request: message is not modified: specified new message content "
        "and reply markup are exactly the same",
    }

    client.edit_message_text(42, 7, "same text")


def test_a_refused_call_raises_without_the_token_in_the_message(fake):
    client = TelegramClient("123:secret-token", base_url=fake.url)
    fake.fail_next = {"error_code": 400, "description": "Bad Request: chat not found"}

    with pytest.raises(TelegramError) as raised:
        client.send_message(42, "hello")

    assert "chat not found" in str(raised.value)
    assert "secret-token" not in str(raised.value)


def test_an_unreachable_api_raises_without_the_token_in_the_message():
    client = TelegramClient("123:secret-token", base_url="http://127.0.0.1:1")

    with pytest.raises(TelegramError) as raised:
        client.send_message(42, "hello")

    assert "secret-token" not in str(raised.value)
