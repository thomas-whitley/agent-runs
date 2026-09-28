"""A fake of the Telegram Bot API on a real local socket.

It answers sendMessage and editMessageText the way Telegram does, with
message ids counting up from 1, and records every call so a test can assert
on what the bot said. Point TELEGRAM_API_URL at `url` to use it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_message_id = 1
        self._lock = threading.Lock()
        # Set to make the next call fail the way Telegram does.
        self.fail_next: dict | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                # /bot<token>/<method>
                _, bot, method = self.path.split("/", 2)
                body = fake._answer(bot.removeprefix("bot"), method, payload)
                data = json.dumps(body).encode()
                self.send_response(200 if body["ok"] else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.tokens: list[str] = []
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _answer(self, token: str, method: str, payload: dict) -> dict:
        with self._lock:
            self.tokens.append(token)
            self.calls.append((method, payload))
            if self.fail_next is not None:
                failure, self.fail_next = self.fail_next, None
                return {"ok": False, **failure}
            if method == "sendMessage":
                message_id = self._next_message_id
                self._next_message_id += 1
                return {"ok": True, "result": {"message_id": message_id, "text": payload["text"]}}
            if method == "editMessageText":
                return {"ok": True, "result": {"message_id": payload["message_id"]}}
            return {"ok": True, "result": True}

    def sent(self, method: str = "sendMessage") -> list[dict]:
        with self._lock:
            return [payload for name, payload in self.calls if name == method]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
