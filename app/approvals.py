"""Approvals: a question the bot asks with inline buttons, per docs/mercury.md.

Each question is one row holding the action, the run or schedule it is about,
the Telegram message and a 24 hour expiry. The button's callback data carries
the row id, and the webhook (app/telegram_webhook.py) answers it. A question
nobody answers is expired by the hourly scheduler Job, and a run waiting on it
ends cancelled.
"""

import logging

import psycopg

from app.runs import cancel_run
from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.approvals")

EXPIRED_NOTE = "Expired after 24 hours with no answer."
APPROVED_NOTE = {
    "start_run": "Approved. Starting.",
    "resume_schedule": "Resumed.",
    "open_anyway": "Opening it anyway.",
}
DECLINED_NOTE = {
    "start_run": "Declined. Cancelled.",
    "resume_schedule": "Left suspended.",
    "open_anyway": "Left closed.",
}

_INSERT = """
INSERT INTO approvals (action, run_id, schedule_name, chat_id, text)
VALUES (%s, %s, %s, %s, %s) RETURNING id
"""
_SET_MESSAGE = "UPDATE approvals SET message_id = %s WHERE id = %s"

# Locks the row, so two presses in quick succession answer it once.
LOCK = """
SELECT action, run_id, schedule_name, chat_id, message_id, text, answer,
       expires_at < now() AS expired
FROM approvals WHERE id = %s FOR UPDATE
"""
ANSWER = "UPDATE approvals SET answer = %s, answered_at = now() WHERE id = %s"
RELEASE_RUN = "UPDATE runs SET status = 'pending' WHERE id = %s AND status = 'awaiting_approval'"
# A new chore carrying the failed one's diff, reporting on the button's message.
OPEN_ANYWAY = """
INSERT INTO runs (
    task, type, provider, repo, source_run_id, telegram_chat_id, telegram_message_id, source
)
SELECT task, 'repo_chore', provider, repo, id, %s, %s, 'telegram' FROM runs WHERE id = %s
RETURNING id
"""

_EXPIRE_DUE = """
UPDATE approvals SET answer = 'expired', answered_at = now()
WHERE answer IS NULL AND expires_at < now()
RETURNING action, run_id, chat_id, message_id, text
"""


def buttons(approval_id: int, action: str) -> list[tuple[str, str]]:
    yes = ("Approve", f"approval:{approval_id}:yes")
    no = f"approval:{approval_id}:no"
    if action == "resume_schedule":
        # Declining a resume changes nothing, so it gets no button.
        return [("Resume", yes[1])]
    if action == "open_anyway":
        return [("Open it anyway", yes[1]), ("Leave it", no)]
    return [yes, ("Decline", no)]


def ask(
    conn: psycopg.Connection,
    telegram: TelegramClient,
    chat_id: int,
    action: str,
    text: str,
    *,
    run_id: str | None = None,
    schedule_name: str | None = None,
) -> int:
    """Store the question, send it with its buttons, and return its id."""
    approval_id = conn.execute(_INSERT, (action, run_id, schedule_name, chat_id, text)).fetchone()[
        0
    ]
    message_id = telegram.send_message(chat_id, text, buttons(approval_id, action))
    conn.execute(_SET_MESSAGE, (message_id, approval_id))
    return approval_id


def expire_due(conn: psycopg.Connection, telegram: TelegramClient | None) -> int:
    """Expire every unanswered question past its expiry. Returns how many."""
    with conn.transaction():
        rows = conn.execute(_EXPIRE_DUE).fetchall()
        for action, run_id, _, _, _ in rows:
            if action == "start_run":
                cancel_run(conn, str(run_id))
    for _, _, chat_id, message_id, text in rows:
        if telegram is None or message_id is None:
            continue
        try:
            telegram.edit_message_text(chat_id, message_id, f"{text}\n\n{EXPIRED_NOTE}")
        except TelegramError as error:
            logger.error("could not mark an approval expired in the chat: %s", error)
    if rows:
        logger.info("expired %s approval(s)", len(rows))
    return len(rows)
