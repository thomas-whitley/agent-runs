"""A chat run: one message from the allowed Telegram chat, turned into a task.

The model sees the task types it may start, with their inputs, and the last
turns of the conversation. It answers with JSON: either a run to create or
one clarifying question. The created run takes over the chat's placeholder
message, so its progress lands in the same place. Nothing here logs the
message or the model's reply.
"""

import json
import logging
import re
from typing import Any

import psycopg
from pydantic import ValidationError

from app.approvals import ask
from app.checks import check_site
from app.loop import (
    DEFAULT_MODEL_RETRY_ATTEMPTS,
    DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
    _complete_with_retry,
)
from app.mercury_config import RepoConfig
from app.model import Model
from app.run_request import RunRequest
from app.runs import finish_run, record_step
from app.tasks import TASK_TYPES
from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.chat")

MAX_TURNS = 20
MODEL_DOWN = "The model is not answering right now. Try again in a few minutes."
CANNOT = "I could not work out a task from that. Try again, or use /runs, /status or /cancel."
NOT_YET = {
    "digest": "The digest is not wired up yet.",
}
WAITING = "That one needs your approval, below."
NO_REPOS = "No repos are listed for chores in mercury.yaml."
# The types a message may start. chat itself is not one of them.
CREATABLE = ("pytest", "site_check")

SYSTEM = """You turn one message from the owner of a small agent service into one task, \
or ask one short clarifying question when you cannot. Answer with a single JSON object \
and nothing else.

Task types you may start:
- pytest: inputs.task is a complete pytest file. The agent writes solution.py until it passes.
- site_check: inputs.task is a URL. inputs.kind is uptime, lighthouse or broken_links; \
uptime when the message does not say.
- repo_chore: inputs.task is an instruction and inputs.repo is owner/name, one of the \
repos listed with the message. It opens a pull request after the owner approves it.
- digest: a summary of the last day. Not available yet.

To start a task: {"action": "create", "type": "<type>", "inputs": {...}}
To ask: {"action": "ask", "question": "<one question>"}"""

_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_HISTORY = """
SELECT role, text FROM (
    SELECT id, role, text FROM telegram_turns WHERE chat_id = %s ORDER BY id DESC LIMIT %s
) recent ORDER BY id
"""
_ADD_TURN = "INSERT INTO telegram_turns (chat_id, role, text) VALUES (%s, %s, %s)"
_PRUNE = """
DELETE FROM telegram_turns WHERE chat_id = %s AND id NOT IN (
    SELECT id FROM telegram_turns WHERE chat_id = %s ORDER BY id DESC LIMIT %s
)
"""
_CREATE = """
INSERT INTO runs (task, type, provider, check_kind, telegram_chat_id, telegram_message_id)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
"""
# A chore waits where no worker claims it, on the question's message, so
# its progress replaces the question once it is approved.
_CREATE_CHORE = """
INSERT INTO runs (task, type, provider, repo, status, telegram_chat_id)
VALUES (%s, 'repo_chore', %s, %s, 'awaiting_approval', %s) RETURNING id
"""
_SET_MESSAGE_FROM_APPROVAL = """
UPDATE runs SET telegram_message_id = (SELECT message_id FROM approvals WHERE id = %s)
WHERE id = %s
"""


def _prompt(
    history: list[tuple[str, str]], message: str, repos: tuple[RepoConfig, ...] = ()
) -> str:
    lines = [f"{role.capitalize()}: {text}" for role, text in history]
    earlier = "\n".join(lines) if lines else "(none)"
    names = ", ".join(repo.name for repo in repos) or "(none)"
    return (
        f"Repos a repo_chore may touch: {names}\n\n"
        f"Conversation so far, oldest first:\n{earlier}\n\nNew message:\n{message}"
    )


def _parse(text: str) -> dict[str, Any] | None:
    match = _OBJECT.search(text)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _create(
    conn,
    intent: dict,
    chat_id: int,
    message_id: int,
    repos: tuple[RepoConfig, ...] = (),
    telegram: TelegramClient | None = None,
) -> tuple[str | None, str]:
    """Create the run the model picked. Returns its id and the reply to show."""
    type_ = intent.get("type")
    if type_ in NOT_YET:
        return None, NOT_YET[type_]
    if type_ == "repo_chore":
        return _create_chore(conn, intent.get("inputs") or {}, chat_id, repos, telegram)
    if type_ not in CREATABLE:
        return None, CANNOT
    try:
        run = RunRequest(type=type_, inputs=intent.get("inputs") or {})
    except ValidationError:
        return None, CANNOT
    run_id = conn.execute(
        _CREATE,
        (
            run.inputs["task"].strip(),
            run.type,
            TASK_TYPES[run.type].provider,
            run.check_kind,
            chat_id,
            message_id,
        ),
    ).fetchone()[0]
    if run.check_kind == "uptime":
        return str(run_id), _run_uptime_check(conn, str(run_id), run.inputs["task"].strip())
    return str(run_id), f"Started a {run.type} run, {str(run_id)[:8]}."


def _run_uptime_check(conn, run_id: str, url: str) -> str:
    """No worker claims an uptime check, because the scheduler runs and
    closes its own. One a chat creates is run here, inside the chat run, the
    way the scheduler would, and the reply is its result."""
    result = check_site(url)
    output = {
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
        "passed": result.passed,
        "error": result.error,
    }
    status = "succeeded" if result.passed else "failed"
    with conn.transaction():
        record_step(conn, run_id, 1, "check", output=output)
        record_step(conn, run_id, 2, "done", output={"status": status})
        finish_run(conn, run_id, status, 0)
    if result.passed:
        return f"{url} is up, {result.status_code} in {round(result.latency_ms)} ms."
    reason = f"HTTP {result.status_code}" if result.status_code else "no answer"
    return f"{url} is down, {reason}."


def _create_chore(
    conn,
    inputs: dict,
    chat_id: int,
    repos: tuple[RepoConfig, ...],
    telegram: TelegramClient | None,
) -> tuple[str | None, str]:
    """A chore is created waiting and asked about, per docs/mercury.md: it
    echoes the repo and the instruction and starts on a button press only."""
    instruction, name = inputs.get("task"), inputs.get("repo")
    if not isinstance(instruction, str) or not instruction.strip():
        return None, CANNOT
    if not repos:
        return None, NO_REPOS
    repo = next((repo for repo in repos if repo.name == name), None)
    if repo is None:
        return None, "I can only work on " + ", ".join(r.name for r in repos) + "."
    if not repo.test_command:
        return None, f"{repo.name} has no test_command in mercury.yaml, so it cannot have a chore."
    if telegram is None:
        return None, CANNOT
    instruction = instruction.strip()
    question = (
        f"Repo chore on {repo.name}:\n{instruction}\n\n"
        f"It runs `{repo.test_command}` and opens a pull request only if that passes."
    )
    try:
        # One transaction, so a question that could not be sent leaves no
        # run waiting on an answer nobody can give.
        with conn.transaction():
            run_id = conn.execute(
                _CREATE_CHORE, (instruction, TASK_TYPES["repo_chore"].provider, repo.name, chat_id)
            ).fetchone()[0]
            approval_id = ask(conn, telegram, chat_id, "start_run", question, run_id=str(run_id))
            conn.execute(_SET_MESSAGE_FROM_APPROVAL, (approval_id, run_id))
    except TelegramError as error:
        logger.error("could not ask about a repo chore: %s", error)
        return None, "I could not send the approval question, so nothing was started."
    return str(run_id), WAITING


def run_chat(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    telegram: TelegramClient | None,
    worker_id: str | None = None,
    repos: tuple[RepoConfig, ...] = (),
    retry_attempts: int = DEFAULT_MODEL_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
) -> int:
    """Answer one chat run and return the tokens it used."""
    message, chat_id, message_id = conn.execute(
        "SELECT task, telegram_chat_id, telegram_message_id FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    history = conn.execute(_HISTORY, (chat_id, MAX_TURNS)).fetchall()
    fields = {"run_id": run_id, "worker_id": worker_id}

    # The same retry the agent loop uses: four tries with 2, 4 and 6 second
    # waits, which fits inside the lease this run does not heartbeat during.
    try:
        reply = _complete_with_retry(
            model, SYSTEM, _prompt(history, message, repos), retry_attempts, retry_backoff_seconds
        )
    except RuntimeError as error:
        # Closed rather than left running, or the worker would take it again
        # every time the lease ran out, and the chat would never hear back.
        logger.error("chat run %s: %s", run_id, error, extra=fields)
        record_step(conn, run_id, 1, "done", output={"status": "error"}, worker_id=worker_id)
        finish_run(conn, run_id, "error", 0, worker_id=worker_id)
        _reply(telegram, chat_id, message_id, MODEL_DOWN, run_id, fields)
        return 0
    intent = _parse(reply.text) or {}

    created: str | None = None
    if intent.get("action") == "create":
        created, answer = _create(conn, intent, chat_id, message_id, repos, telegram)
    elif intent.get("action") == "ask" and isinstance(intent.get("question"), str):
        answer = intent["question"].strip() or CANNOT
    else:
        answer = CANNOT

    with conn.transaction():
        conn.execute(_ADD_TURN, (chat_id, "user", message))
        conn.execute(_ADD_TURN, (chat_id, "assistant", answer))
        conn.execute(_PRUNE, (chat_id, chat_id, MAX_TURNS))

    outcome = {"action": intent.get("action"), "type": intent.get("type"), "run_id": created}
    record_step(conn, run_id, 1, "intent", output=outcome, tokens=reply.tokens, worker_id=worker_id)
    record_step(conn, run_id, 2, "done", output={"status": "succeeded"}, worker_id=worker_id)
    finish_run(conn, run_id, "succeeded", reply.tokens, worker_id=worker_id)

    action = outcome["action"] or "nothing"
    logger.info("chat run %s answered with %s", run_id, action, extra=fields)
    _reply(telegram, chat_id, message_id, answer, run_id, fields)
    return reply.tokens


def _reply(telegram, chat_id, message_id, text: str, run_id: str, fields: dict) -> None:
    if telegram is None:
        logger.error("no TELEGRAM_BOT_TOKEN, so chat run %s could not reply", run_id, extra=fields)
        return
    try:
        telegram.edit_message_text(chat_id, message_id, text)
    except TelegramError as error:
        logger.error("chat run %s could not reply: %s", run_id, error, extra=fields)
