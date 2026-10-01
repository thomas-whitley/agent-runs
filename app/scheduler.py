"""ROLE=scheduler: a Container Apps Job that wakes on a cron trigger, checks
the configured sites, creates any weekly browser check that is due, and
exits. It creates each uptime run through the API with the bearer token, so
the run exists with no client attached to watch it, then executes and closes
the run itself in the same process. Each run also applies the retention
cleanup (app/cleanup.py). A plain HTTP check needs no browser and
no separate worker, and the agent loop worker never claims uptime runs (see
_CLAIMABLE in app/worker.py).

The CI watch works the same way, one ci_watch run per configured repo,
read from the GitHub Actions API, and so does the weekly dependency audit
(app/audit.py). Once a day it creates the digest run, which the worker
writes and sends (app/digest.py).

The weekly lighthouse and broken_links checks are only created here. The
self hosted checks worker claims them, and a lighthouse check nobody claims
within the window falls back to PageSpeed on the Python worker.
"""

import json
import logging
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

import psycopg
from opentelemetry import trace

from app.approvals import ask, expire_due
from app.audit import OSVClient, OSVError, audit_repo
from app.checks import check_site
from app.cleanup import run_cleanup
from app.config import load_settings
from app.github import GitHubClient, GitHubError
from app.logging_setup import configure_logging
from app.mercury_config import load_mercury_config
from app.migrations import apply_migrations
from app.runs import finish_run, record_step
from app.schedule_state import SUSPEND_AFTER, is_suspended, record_failure, record_success
from app.tasks import SCHEDULER_CHECK_KINDS, SELF_HOSTED_CHECK_KINDS
from app.telegram import TelegramClient, TelegramError, telegram_client
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.scheduler")

# The api scales to zero, and an hourly Job almost always finds it there.
# Container Apps holds the first request while a replica starts, so this has
# to cover a cold start, not only a request. The Job's replicaTimeout is 300.
CREATE_RUN_TIMEOUT_SECONDS = 60.0

# The ingress can drop the scheduler's POST while the api cold starts and
# then deliver it, so the api writes a run the scheduler never heard about,
# and it stays pending with nobody to close it. Each hourly run closes any
# run of the scheduler's own kinds left pending and unclaimed this long. No
# worker claims those kinds, so nothing else would.
_CLOSE_ORPHANED_UPTIME_RUNS = """
SELECT id::text FROM runs
WHERE type = 'site_check' AND coalesce(check_kind, 'uptime') = ANY(%s)
  AND finished_at IS NULL AND claimed_by IS NULL
  AND created_at < now() - interval '10 minutes'
"""
ORPHAN_REASON = (
    "the scheduler's request to create this run failed, so the scheduler never ran the check"
)


def close_orphaned_uptime_runs(conn: psycopg.Connection) -> list[str]:
    orphans = [
        row[0] for row in conn.execute(_CLOSE_ORPHANED_UPTIME_RUNS, (list(SCHEDULER_CHECK_KINDS),))
    ]
    for run_id in orphans:
        record_step(conn, run_id, 1, "done", output={"status": "error", "reason": ORPHAN_REASON})
        finish_run(conn, run_id, "error", 0)
        logger.warning("closed orphaned uptime run %s", run_id, extra={"run_id": run_id})
    return orphans


# A weekly check is due when none of its kind for its page was created in
# this long. Counting from the last one, rather than reading a cron day,
# means an hourly Job that missed its slot catches up on the next run.
WEEKLY_INTERVAL = "7 days"

_LAST_CHECK_IS_RECENT = """
SELECT EXISTS (
    SELECT 1 FROM runs
    WHERE type = 'site_check' AND check_kind = %s AND task = %s
      AND created_at > now() - %s::interval
)
"""


def _create_run(
    api_base_url: str,
    bearer_token: str,
    url: str,
    timeout_seconds: float = CREATE_RUN_TIMEOUT_SECONDS,
    kind: str | None = None,
    run_type: str = "site_check",
) -> str:
    inputs = {"task": url} if kind is None else {"task": url, "kind": kind}
    body = json.dumps({"type": run_type, "inputs": inputs}).encode()
    request = urllib.request.Request(
        f"{api_base_url}/runs",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())["id"]


def run_due_checks(
    conn: psycopg.Connection,
    sites: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> list[str]:
    """Check every configured site not currently suspended. Returns the ids
    of the runs created, so a caller (or a test) can inspect them. With a
    Telegram client and chat, the failure that suspends a site sends one
    message with a Resume button."""
    created: list[str] = []
    close_orphaned_uptime_runs(conn)

    for url in sites:
        schedule_name = f"site_uptime:{url}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue

        # OSError covers URLError, HTTPError (a 401 from a wrong token) and a
        # timeout while reading the response. It counts as a failure, so a bad
        # token or a dead api suspends the schedule rather than skipping it
        # every hour with nothing to show that checks have stopped.
        try:
            run_id = _create_run(api_base_url, bearer_token, url)
        except OSError:
            logger.exception("could not create a run for %s, counted as a failure", url)
            _fail(conn, schedule_name, f"{url}'s hourly check", telegram, chat_id)
            continue

        logger.info("scheduled run %s created with no client", run_id, extra={"run_id": run_id})
        created.append(run_id)

        result = check_site(url)
        record_step(
            conn,
            run_id,
            1,
            "check",
            output={
                "status_code": result.status_code,
                "latency_ms": result.latency_ms,
                "passed": result.passed,
                "error": result.error,
            },
        )
        finish_run(conn, run_id, "succeeded" if result.passed else "failed", 0)

        if result.passed:
            record_success(conn, schedule_name)
        else:
            _fail(conn, schedule_name, f"{url}'s hourly check", telegram, chat_id)

    return created


def _fail(
    conn: psycopg.Connection,
    schedule_name: str,
    what: str,
    telegram: TelegramClient | None,
    chat_id: int | None,
) -> None:
    """Count one failure of a schedule entry. The one that suspends it sends a
    message with a Resume button. `what` names the check in that message."""
    if not record_failure(conn, schedule_name) or telegram is None or chat_id is None:
        return
    text = f"{what} failed {SUSPEND_AFTER} times in a row, so it is suspended."
    try:
        ask(conn, telegram, chat_id, "resume_schedule", text, schedule_name=schedule_name)
    except TelegramError as error:
        # The suspension stands and /status shows it; only the message is lost.
        logger.error("could not send the suspension message: %s", error)


def schedule_weekly_checks(
    conn: psycopg.Connection,
    pages: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
) -> list[str]:
    """Create a lighthouse and a broken_links check for every page that has
    not had one of that kind this week. Returns the ids of the runs created."""
    created: list[str] = []

    for url in pages:
        for kind in SELF_HOSTED_CHECK_KINDS:
            recent = conn.execute(_LAST_CHECK_IS_RECENT, (kind, url, WEEKLY_INTERVAL)).fetchone()[0]
            if recent:
                continue
            # Not counted against a schedule: the uptime checks already
            # suspend on a refused POST, and the next hour tries again.
            try:
                run_id = _create_run(api_base_url, bearer_token, url, kind=kind)
            except OSError:
                logger.exception("could not create a weekly %s check for %s", kind, url)
                continue
            logger.info(
                "scheduled weekly %s check %s for %s", kind, run_id, url, extra={"run_id": run_id}
            )
            created.append(run_id)

    return created


# A workflow whose newest completed run on the default branch ended in one of
# these is failing. cancelled and skipped are not failures.
FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

# A failure already announced, or any failure inside 24 hours of the last
# message, goes in the digest instead of sending another message.
_LAST_CI_FINDING = """
SELECT s.output FROM runs r JOIN steps s ON s.run_id = r.id AND s.seq = 1
WHERE r.type = 'site_check' AND r.check_kind = 'ci_watch' AND r.task = %s AND r.id <> %s
ORDER BY r.created_at DESC LIMIT 1
"""
_CI_MESSAGE_SENT_RECENTLY = """
SELECT EXISTS (
    SELECT 1 FROM runs r JOIN steps s ON s.run_id = r.id AND s.seq = 1
    WHERE r.type = 'site_check' AND r.check_kind = 'ci_watch' AND r.task = %s
      AND r.created_at > now() - interval '24 hours'
      AND (s.output ->> 'notified')::boolean
)
"""


def ci_finding(github: GitHubClient, repo: str) -> dict:
    """The newest completed run of each workflow on the repo's default
    branch, and the ones that failed."""
    branch = github.default_branch(repo)
    newest: dict[str, dict] = {}
    for run in github.completed_workflow_runs(repo, branch):
        newest.setdefault(run["name"], run)
    failing = [
        {
            "workflow": run["name"],
            "id": run["id"],
            "conclusion": run["conclusion"],
            "sha": run["head_sha"][:7],
            "url": run["html_url"],
        }
        for run in newest.values()
        if run["conclusion"] in FAILED_CONCLUSIONS
    ]
    return {"repo": repo, "branch": branch, "workflows": len(newest), "failing": failing}


def watch_ci(
    conn: psycopg.Connection,
    repos: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
    github: GitHubClient,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> list[str]:
    """Create and close one ci_watch check per repo. A red CI is a finding,
    not a failed check. Only a GitHub call that fails counts towards
    suspending the watch. Returns the ids of the runs created."""
    created: list[str] = []

    for repo in repos:
        schedule_name = f"ci_watch:{repo}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue
        what = f"The CI watch for {repo}"
        try:
            run_id = _create_run(api_base_url, bearer_token, repo, kind="ci_watch")
        except OSError:
            logger.exception("could not create a ci_watch run for %s, counted as a failure", repo)
            _fail(conn, schedule_name, what, telegram, chat_id)
            continue
        created.append(run_id)

        try:
            finding = ci_finding(github, repo)
        except GitHubError as error:
            logger.error("ci_watch %s: %s", run_id, error, extra={"run_id": run_id})
            _close_check(conn, run_id, {"repo": repo, "error": str(error)}, "failed")
            _fail(conn, schedule_name, what, telegram, chat_id)
            continue

        finding["notified"] = _announce_ci_failure(conn, run_id, finding, telegram, chat_id)
        _close_check(conn, run_id, finding, "succeeded")
        record_success(conn, schedule_name)
        logger.info(
            "ci_watch %s: %s of %s workflow(s) failing on %s",
            run_id,
            len(finding["failing"]),
            finding["workflows"],
            repo,
            extra={"run_id": run_id},
        )

    return created


def _announce_ci_failure(
    conn: psycopg.Connection,
    run_id: str,
    finding: dict,
    telegram: TelegramClient | None,
    chat_id: int | None,
) -> bool:
    """Send one message for a failure not seen in the previous watch, unless
    one was sent inside 24 hours. True when a message went out."""
    if not finding["failing"] or telegram is None or chat_id is None:
        return False
    repo = finding["repo"]
    last = conn.execute(_LAST_CI_FINDING, (repo, run_id)).fetchone()
    seen = {f["id"] for f in (last[0] or {}).get("failing", [])} if last else set()
    new = [f for f in finding["failing"] if f["id"] not in seen]
    if not new or conn.execute(_CI_MESSAGE_SENT_RECENTLY, (repo,)).fetchone()[0]:
        return False
    lines = [f"CI is failing on {repo} ({finding['branch']}):"]
    lines += [f"{f['workflow']} {f['conclusion']} at {f['sha']}: {f['url']}" for f in new]
    try:
        telegram.send_message(chat_id, "\n".join(lines))
    except TelegramError as error:
        logger.error("could not send the CI failure message: %s", error)
        return False
    return True


def audit_dependencies(
    conn: psycopg.Connection,
    repos: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
    github: GitHubClient,
    osv: OSVClient,
    *,
    telegram: TelegramClient | None = None,
    chat_id: int | None = None,
) -> list[str]:
    """Create and close a dependency_audit check for every repo that has not
    had one this week. Findings go in the digest only. A GitHub or OSV call
    that fails counts towards suspending the audit, and Telegram hears only
    of that. Returns the ids of the runs created."""
    created: list[str] = []

    for repo in repos:
        schedule_name = f"dependency_audit:{repo}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue
        recent = conn.execute(
            _LAST_CHECK_IS_RECENT, ("dependency_audit", repo, WEEKLY_INTERVAL)
        ).fetchone()[0]
        if recent:
            continue
        what = f"The dependency audit for {repo}"
        try:
            run_id = _create_run(api_base_url, bearer_token, repo, kind="dependency_audit")
        except OSError:
            logger.exception("could not create a dependency_audit run for %s", repo)
            _fail(conn, schedule_name, what, telegram, chat_id)
            continue
        created.append(run_id)

        try:
            finding = audit_repo(github, osv, repo)
        except (GitHubError, OSVError, ValueError) as error:
            # ValueError is a lock file that would not parse.
            logger.error("dependency_audit %s: %s", run_id, error, extra={"run_id": run_id})
            _close_check(conn, run_id, {"repo": repo, "error": str(error)}, "failed")
            _fail(conn, schedule_name, what, telegram, chat_id)
            continue

        _close_check(conn, run_id, finding, "succeeded")
        record_success(conn, schedule_name)
        logger.info(
            "dependency_audit %s: %s vulnerable package(s) in %s lock file(s) of %s",
            run_id,
            finding["vulnerable_count"],
            len(finding["lock_files"]),
            repo,
            extra={"run_id": run_id},
        )

    return created


# The digest is due at this local time. The Job fires on the hour in UTC,
# so the first tick after it, 08:00 local, creates it, whatever daylight
# saving is doing.
DIGEST_LOCAL_TIME = time(7, 30)

_DIGEST_SINCE = "SELECT EXISTS (SELECT 1 FROM runs WHERE type = 'digest' AND created_at >= %s)"


def schedule_digest(
    conn: psycopg.Connection,
    api_base_url: str,
    bearer_token: str,
    timezone: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Create today's digest run once it is due and none has been created
    since today's due time. The worker writes and sends it. Returns the run
    id, or None when nothing was created."""
    zone = ZoneInfo(timezone)
    local = (now or datetime.now(zone)).astimezone(zone)
    due = datetime.combine(local.date(), DIGEST_LOCAL_TIME, tzinfo=zone)
    if local < due or conn.execute(_DIGEST_SINCE, (due,)).fetchone()[0]:
        return None
    try:
        run_id = _create_run(
            api_base_url, bearer_token, local.date().isoformat(), run_type="digest"
        )
    except OSError:
        # The next hourly tick tries again.
        logger.exception("could not create the digest run")
        return None
    logger.info("scheduled the digest %s for %s", run_id, local.date(), extra={"run_id": run_id})
    return run_id


def _close_check(conn: psycopg.Connection, run_id: str, finding: dict, status: str) -> None:
    """The finding as step 1 and a done event as step 2, the shape a posted
    check result has, so the run's stream ends."""
    with conn.transaction():
        record_step(conn, run_id, 1, "check", output=finding)
        record_step(conn, run_id, 2, "done", output={"status": status})
        finish_run(conn, run_id, status, 0)


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    tracing_on = configure_telemetry()

    if not settings.mercury_bearer_token:
        raise RuntimeError("no MERCURY_BEARER_TOKEN: the scheduler cannot call the api")

    config = load_mercury_config(settings.mercury_config_path)

    telegram = telegram_client(settings)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        expire_due(conn, telegram)
        created = run_due_checks(
            conn,
            config.sites,
            settings.api_base_url,
            settings.mercury_bearer_token,
            telegram=telegram,
            chat_id=config.telegram_chat_id,
        )
        created += schedule_weekly_checks(
            conn, config.pages, settings.api_base_url, settings.mercury_bearer_token
        )
        repos = tuple(repo.name for repo in config.repos)
        github = GitHubClient(settings.mercury_github_token, settings.github_api_url)
        created += watch_ci(
            conn,
            repos,
            settings.api_base_url,
            settings.mercury_bearer_token,
            github,
            telegram=telegram,
            chat_id=config.telegram_chat_id,
        )
        created += audit_dependencies(
            conn,
            repos,
            settings.api_base_url,
            settings.mercury_bearer_token,
            github,
            OSVClient(settings.osv_api_url),
            telegram=telegram,
            chat_id=config.telegram_chat_id,
        )
        digest = schedule_digest(
            conn, settings.api_base_url, settings.mercury_bearer_token, config.timezone
        )
        created += [digest] if digest else []
        run_cleanup(conn, config.event_bodies_days, config.runs_days)

    logger.info("scheduler run complete, %s check(s) created", len(created))

    # The container exits within seconds of this returning. A batch span
    # processor's queue would be dropped unsent without an explicit flush.
    if tracing_on:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()


if __name__ == "__main__":  # pragma: no cover
    main()
