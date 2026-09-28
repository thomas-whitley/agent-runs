"""The outbound half of the Telegram bot: send a message, edit one in place.

The token is part of every request path, so nothing here puts a URL in an
exception or a log line. Errors carry Telegram's own description only.
"""

import json
import urllib.error
import urllib.request

DEFAULT_API_URL = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, base_url: str = DEFAULT_API_URL) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def send_message(self, chat_id: int, text: str) -> int:
        """Send text to the chat and return the new message's id."""
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return result["message_id"]

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a message's text. Unchanged text is not an error."""
        try:
            self._call(
                "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text}
            )
        except TelegramError as error:
            if "message is not modified" not in str(error):
                raise

    def _call(self, method: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self._base_url}/bot{self._token}/{method}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = _json_or_empty(error.read())
            description = body.get("description", f"HTTP {error.code}")
            raise TelegramError(f"{method} refused: {description}") from None
        except (OSError, ValueError) as error:
            # str(URLError) can carry the URL, and the URL carries the token.
            raise TelegramError(f"{method} failed: {type(error).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method} refused: {body.get('description', 'no description')}")
        return body["result"]


def _json_or_empty(raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except ValueError:
        return {}
