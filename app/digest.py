"""The daily digest. The worker gathers the last day's check results into a
bounded set of facts, has the digest type's model write a short message from
them, and sends it to the owner's chat. If the model never answers, the facts
are rendered as plain text and sent instead, so a provider outage costs the
wording and not the digest.

The weekly kinds (Lighthouse, broken links, the dependency audit) are read
from the last 8 days rather than the last day, so the latest one is always in
the digest, beside the one before it for a week over week comparison.
"""

import json
import logging

import psycopg

from app.loop import (
    DEFAULT_MODEL_RETRY_ATTEMPTS,
    DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
    _complete_with_retry,
)
from app.model import Model
from app.runs import finish_run, record_step
from app.telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_runs.digest")

DAY = "24 hours"
WEEK = "8 days"
# Telegram refuses a message over 4,096 characters.
MAX_MESSAGE_CHARS = 4000
# Bounds the facts, and so the prompt, whatever the checks found.
MAX_LISTED = 10

SYSTEM = """You write a short daily status message for the owner of a few websites and \
code repositories. You are given facts as JSON. Use only those facts and never invent a \
number, a name or a cause. Lead with anything that needs attention: a site that failed a \
check, a Lighthouse score that dropped since the previous run, broken links, failing CI, \
vulnerable dependencies, a suspended schedule, a check that errored. Then say in one line \
what was fine. Plain text, no markdown, at most 1,200 characters."""

_UPTIME = """
SELECT r.task, count(*), count(*) FILTER (WHERE r.status <> 'succeeded'),
       round(max((s.output ->> 'latency_ms')::numeric))::int,
       (array_agg((s.output ->> 'status_code')::int ORDER BY r.created_at DESC))[1]
FROM runs r LEFT JOIN steps s ON s.run_id = r.id AND s.seq = 1
WHERE r.type = 'site_check' AND coalesce(r.check_kind, 'uptime') = 'uptime'
  AND r.finished_at IS NOT NULL AND r.created_at > now() - %s::interval
GROUP BY r.task ORDER BY r.task
"""

# The newest finished results of one kind per task, newest first, two each,
# so the latest can sit beside the one before it.
_LATEST = """
SELECT task, output, rank FROM (
    SELECT r.task, s.output,
           row_number() OVER (PARTITION BY r.task ORDER BY r.created_at DESC) AS rank
    FROM runs r JOIN steps s ON s.run_id = r.id AND s.seq = 1
    WHERE r.type = 'site_check' AND r.check_kind = %s
      AND r.finished_at IS NOT NULL AND r.created_at > now() - %s::interval
) latest
WHERE rank <= 2 ORDER BY task, rank
"""

_CI_MESSAGED = """
SELECT DISTINCT r.task FROM runs r JOIN steps s ON s.run_id = r.id AND s.seq = 1
WHERE r.type = 'site_check' AND r.check_kind = 'ci_watch'
  AND r.created_at > now() - %s::interval AND (s.output ->> 'notified')::boolean
"""

_SUSPENDED = "SELECT name FROM schedule_state WHERE suspended ORDER BY name"

_RUNS = """
SELECT type, status, count(*) FROM runs
WHERE type NOT IN ('site_check', 'digest') AND created_at > now() - %s::interval
GROUP BY type, status ORDER BY type, status
"""


def _latest(conn: psycopg.Connection, kind: str, interval: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for task, output, _ in conn.execute(_LATEST, (kind, interval)):
        found.setdefault(task, []).append(output or {})
    return found


def gather_facts(conn: psycopg.Connection) -> dict:
    """What the digest says, as JSON safe data with every list bounded."""
    uptime = [
        {
            "site": site,
            "checks": checks,
            "failed": failed,
            "slowest_ms": slowest,
            "last_status_code": last_status,
        }
        for site, checks, failed, slowest, last_status in conn.execute(_UPTIME, (DAY,))
    ]

    lighthouse = []
    for page, (latest, *previous) in _latest(conn, "lighthouse", WEEK).items():
        if "error" in latest:
            lighthouse.append({"page": page, "error": latest["error"]})
            continue
        before = previous[0] if previous and "error" not in previous[0] else {}
        lighthouse.append(
            {
                "page": page,
                "scores": latest.get("scores"),
                "previous_scores": before.get("scores"),
                "lcp_ms": latest.get("lcp_ms"),
                "previous_lcp_ms": before.get("lcp_ms"),
                "failed_audits": (latest.get("failed_audits") or [])[:MAX_LISTED],
            }
        )

    broken_links = []
    for page, (latest, *_) in _latest(conn, "broken_links", WEEK).items():
        if "error" in latest:
            broken_links.append({"page": page, "error": latest["error"]})
            continue
        broken_links.append(
            {
                "page": page,
                "pages_checked": latest.get("pages_checked"),
                "broken_count": latest.get("broken_count"),
                "broken": (latest.get("broken") or [])[:MAX_LISTED],
            }
        )

    messaged = {row[0] for row in conn.execute(_CI_MESSAGED, (DAY,))}
    ci = []
    for repo, (latest, *_) in _latest(conn, "ci_watch", DAY).items():
        if "error" in latest:
            ci.append({"repo": repo, "error": latest["error"]})
            continue
        failing = [
            {key: f[key] for key in ("workflow", "conclusion", "sha", "url")}
            for f in latest.get("failing", [])[:MAX_LISTED]
        ]
        if failing:
            ci.append({"repo": repo, "failing": failing, "already_messaged": repo in messaged})

    dependencies = []
    for repo, (latest, *_) in _latest(conn, "dependency_audit", WEEK).items():
        if "error" in latest:
            dependencies.append({"repo": repo, "error": latest["error"]})
            continue
        advisories = latest.get("advisories", {})
        listed = [
            {
                "id": advisory_id,
                "package": f"{entry['package']} {entry['version']}",
                "severity": advisories.get(advisory_id, {}).get("severity"),
                "summary": advisories.get(advisory_id, {}).get("summary"),
            }
            for entry in latest.get("vulnerable", [])
            for advisory_id in entry["ids"]
        ]
        dependencies.append(
            {
                "repo": repo,
                "vulnerable_count": latest.get("vulnerable_count", 0),
                "advisories": listed[:MAX_LISTED],
            }
        )

    runs: dict[str, dict[str, int]] = {}
    for run_type, status, count in conn.execute(_RUNS, (DAY,)):
        runs.setdefault(run_type, {})[status] = count

    return {
        "uptime": uptime,
        "lighthouse": lighthouse,
        "broken_links": broken_links,
        "ci": ci,
        "dependencies": dependencies,
        "suspended": [row[0] for row in conn.execute(_SUSPENDED)],
        "runs": runs,
    }


def _scores(scores: dict | None) -> str:
    if not scores:
        return "no scores"
    return ", ".join(f"{name} {score}" for name, score in scores.items())


def render_facts(facts: dict) -> str:
    """The facts as plain text, sent when the model does not answer."""
    lines = ["Mercury digest"]
    for site in facts["uptime"]:
        lines.append(
            f"Uptime {site['site']}: {site['failed']} of {site['checks']} checks failed, "
            f"slowest {site['slowest_ms']} ms, last status {site['last_status_code']}."
        )
    for page in facts["lighthouse"]:
        if "error" in page:
            lines.append(f"Lighthouse {page['page']}: the check errored, {page['error']}")
            continue
        line = f"Lighthouse {page['page']}: {_scores(page['scores'])}"
        if page["previous_scores"]:
            line += f" (previous run {_scores(page['previous_scores'])})"
        lines.append(line + ".")
    for page in facts["broken_links"]:
        if "error" in page:
            lines.append(f"Broken links {page['page']}: the check errored, {page['error']}")
            continue
        urls = ", ".join(f"{b['url']} ({b['status']})" for b in page["broken"])
        lines.append(
            f"Broken links {page['page']}: {page['broken_count']} in "
            f"{page['pages_checked']} pages" + (f": {urls}" if urls else ".")
        )
    for repo in facts["ci"]:
        if "error" in repo:
            lines.append(f"CI {repo['repo']}: the watch errored, {repo['error']}")
            continue
        for f in repo["failing"]:
            lines.append(f"CI {repo['repo']}: {f['workflow']} {f['conclusion']} at {f['sha']}.")
    for repo in facts["dependencies"]:
        if "error" in repo:
            lines.append(f"Dependencies {repo['repo']}: the audit errored, {repo['error']}")
            continue
        for a in repo["advisories"]:
            lines.append(
                f"Dependencies {repo['repo']}: {a['package']} has {a['id']} "
                f"({a['severity'] or 'no severity'}), {a['summary']}"
            )
    for name in facts["suspended"]:
        lines.append(f"Suspended: {name}.")
    for run_type, statuses in facts["runs"].items():
        counts = ", ".join(f"{count} {status}" for status, count in statuses.items())
        lines.append(f"Runs {run_type}: {counts}.")
    if len(lines) == 1:
        lines.append("No checks ran in the last day.")
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


def run_digest(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    telegram: TelegramClient | None,
    chat_id: int | None,
    *,
    worker_id: str | None = None,
    retry_attempts: int = DEFAULT_MODEL_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
) -> int:
    """Gather, write and send one digest. Returns the tokens it used."""
    fields = {"run_id": run_id, "worker_id": worker_id}
    facts = gather_facts(conn)
    record_step(conn, run_id, 1, "gather", output=facts, worker_id=worker_id)

    prompt = f"Facts for the last day:\n{json.dumps(facts, indent=1)}"
    try:
        reply = _complete_with_retry(model, SYSTEM, prompt, retry_attempts, retry_backoff_seconds)
        text, source, tokens = reply.text.strip()[:MAX_MESSAGE_CHARS], "model", reply.tokens
    except RuntimeError as error:
        logger.error("digest %s: %s, sending the plain facts", run_id, error, extra=fields)
        text, source, tokens = "", "facts", 0
    if not text:
        text, source = render_facts(facts), "facts"

    sent, reason = _send(telegram, chat_id, text, run_id, fields)
    output = {"text": text, "source": source, "sent": sent}
    record_step(conn, run_id, 2, "write", output=output, tokens=tokens, worker_id=worker_id)
    status = "succeeded" if sent else "error"
    done = {"status": status} if sent else {"status": status, "reason": reason}
    record_step(conn, run_id, 3, "done", output=done, worker_id=worker_id)
    finish_run(conn, run_id, status, tokens, worker_id=worker_id)
    logger.info("digest %s %s, written by the %s", run_id, status, source, extra=fields)
    return tokens


def _send(telegram, chat_id, text: str, run_id: str, fields: dict) -> tuple[bool, str | None]:
    if telegram is None or chat_id is None:
        logger.error("digest %s has no bot token or chat to send to", run_id, extra=fields)
        return False, "no Telegram bot token or chat configured"
    try:
        telegram.send_message(chat_id, text)
    except TelegramError as error:
        logger.error("digest %s could not be sent: %s", run_id, error, extra=fields)
        return False, "Telegram refused the message"
    return True, None
