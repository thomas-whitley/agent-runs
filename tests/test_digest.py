"""The daily digest: the scheduler creates one digest run a day once it is
07:30 in the configured timezone, and the worker gathers the last day's check
results, has the model write a short message from them, and sends it to the
owner's chat. If the model never answers, the plain facts go instead.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.digest import gather_facts, render_facts, run_digest
from app.model import ModelReply, StubModel
from app.runs import claim_run, finish_run, record_step
from app.schedule_state import record_failure
from app.scheduler import schedule_digest
from app.telegram import TelegramClient
from app.worker import process_run
from tests.test_worker import settings_with, stub_model_builder

BEARER_TOKEN = "test-bearer-token"
MELBOURNE = ZoneInfo("Australia/Melbourne")
CHAT_ID = 42
SITE = "https://example.com/health"
PAGE = "https://example.com/"
REPO = "owner/service"


def _check(conn, kind, task, output, *, status="succeeded", hours_ago=1):
    run_id = conn.execute(
        "INSERT INTO runs (task, type, check_kind, created_at) "
        "VALUES (%s, 'site_check', %s, now() - make_interval(hours => %s)) RETURNING id::text",
        (task, kind, hours_ago),
    ).fetchone()[0]
    record_step(conn, run_id, 1, "check", output=output)
    record_step(conn, run_id, 2, "done", output={"status": status})
    finish_run(conn, run_id, status, 0)
    return run_id


def _a_day_of_checks(conn):
    for hours_ago in (1, 2, 3):
        _check(
            conn,
            "uptime",
            SITE,
            {"status_code": 200, "latency_ms": 100 * hours_ago, "passed": True, "error": None},
            hours_ago=hours_ago,
        )
    _check(
        conn,
        "uptime",
        SITE,
        {"status_code": 503, "latency_ms": 40, "passed": False, "error": None},
        status="failed",
        hours_ago=4,
    )
    _check(
        conn,
        "uptime",
        SITE,
        {"status_code": 200, "latency_ms": 90, "passed": True, "error": None},
        hours_ago=30,
    )
    _check(
        conn,
        "lighthouse",
        PAGE,
        {"scores": {"performance": 0.82}, "lcp_ms": 2400, "tbt_ms": 10, "failed_audits": []},
        hours_ago=170,
    )
    _check(
        conn,
        "lighthouse",
        PAGE,
        {
            "scores": {"performance": 0.95},
            "lcp_ms": 1500,
            "tbt_ms": 0,
            "failed_audits": ["image-alt"],
        },
        hours_ago=5,
    )
    _check(
        conn,
        "broken_links",
        PAGE,
        {
            "pages_checked": 12,
            "broken_count": 1,
            "broken": [{"url": f"{PAGE}gone", "status": 404, "found_on": PAGE}],
        },
        hours_ago=6,
    )
    _check(
        conn,
        "ci_watch",
        REPO,
        {
            "repo": REPO,
            "branch": "main",
            "workflows": 3,
            "failing": [
                {
                    "workflow": "CI",
                    "id": 9,
                    "conclusion": "failure",
                    "sha": "abc1234",
                    "url": "https://github.com/owner/service/actions/runs/9",
                }
            ],
            "notified": True,
        },
        hours_ago=2,
    )
    _check(
        conn,
        "dependency_audit",
        REPO,
        {
            "repo": REPO,
            "branch": "main",
            "lock_files": [],
            "vulnerable_count": 1,
            "advisory_count": 1,
            "vulnerable": [
                {
                    "ecosystem": "PyPI",
                    "package": "pyjwt",
                    "version": "2.14.0",
                    "lock_file": "uv.lock",
                    "ids": ["GHSA-42vr"],
                }
            ],
            "advisories": {
                "GHSA-42vr": {
                    "summary": "RecursionError DoS",
                    "severity": "MODERATE",
                    "aliases": [],
                }
            },
        },
        hours_ago=50,
    )
    _check(
        conn,
        "lighthouse",
        "https://example.com/other",
        {"error": "Chrome crashed", "log_tail": "x"},
        hours_ago=3,
    )
    for _ in range(3):
        record_failure(conn, "site_uptime:https://down.example")
    conn.execute(
        "INSERT INTO runs (task, type, status, finished_at)"
        " VALUES ('x', 'pytest', 'succeeded', now())"
    )


def test_the_facts_cover_the_last_day_and_the_latest_weekly_checks(migrated_db):
    _a_day_of_checks(migrated_db)

    facts = gather_facts(migrated_db)

    assert facts["uptime"] == [
        {"site": SITE, "checks": 4, "failed": 1, "slowest_ms": 300, "last_status_code": 200}
    ]
    [main_page, other_page] = sorted(facts["lighthouse"], key=lambda f: f["page"])
    assert main_page["scores"] == {"performance": 0.95}
    assert main_page["previous_scores"] == {"performance": 0.82}
    assert (main_page["lcp_ms"], main_page["previous_lcp_ms"]) == (1500, 2400)
    assert main_page["failed_audits"] == ["image-alt"]
    assert other_page["error"] == "Chrome crashed"
    assert "log_tail" not in other_page
    assert facts["broken_links"] == [
        {
            "page": PAGE,
            "pages_checked": 12,
            "broken_count": 1,
            "broken": [{"url": f"{PAGE}gone", "status": 404, "found_on": PAGE}],
        }
    ]
    assert facts["ci"] == [
        {
            "repo": REPO,
            "failing": [
                {
                    "workflow": "CI",
                    "conclusion": "failure",
                    "sha": "abc1234",
                    "url": "https://github.com/owner/service/actions/runs/9",
                }
            ],
            "already_messaged": True,
        }
    ]
    assert facts["dependencies"] == [
        {
            "repo": REPO,
            "vulnerable_count": 1,
            "advisories": [
                {
                    "id": "GHSA-42vr",
                    "package": "pyjwt 2.14.0",
                    "severity": "MODERATE",
                    "summary": "RecursionError DoS",
                }
            ],
        }
    ]
    assert facts["suspended"] == ["site_uptime:https://down.example"]
    assert facts["runs"] == {"pytest": {"succeeded": 1}}


def test_uptime_latency_is_stored_as_a_float_and_the_facts_round_it(migrated_db):
    """check_site measures latency_ms as a float, as the live rows hold it."""
    _check(
        migrated_db,
        "uptime",
        SITE,
        {"status_code": 200, "latency_ms": 83.62, "passed": True, "error": None},
    )

    [site] = gather_facts(migrated_db)["uptime"]

    assert site["slowest_ms"] == 84


def test_with_nothing_checked_the_facts_are_empty_and_still_render(migrated_db):
    facts = gather_facts(migrated_db)

    assert facts["uptime"] == [] and facts["ci"] == [] and facts["suspended"] == []
    assert "No checks ran" in render_facts(facts)


def test_the_rendered_facts_name_every_problem(migrated_db):
    _a_day_of_checks(migrated_db)

    text = render_facts(gather_facts(migrated_db))

    for expected in (
        SITE,
        "1 of 4",
        "0.82",
        "0.95",
        f"{PAGE}gone",
        "CI",
        "pyjwt 2.14.0",
        "GHSA-42vr",
        "site_uptime:https://down.example",
        "Chrome crashed",
    ):
        assert expected in text, expected


def _digest_run(conn) -> str:
    run_id = conn.execute(
        "INSERT INTO runs (task, type) VALUES ('2026-10-01', 'digest') RETURNING id::text"
    ).fetchone()[0]
    claim_run(conn, run_id, "worker-test")
    return run_id


class _DeadModel:
    def complete(self, system: str, prompt: str) -> ModelReply:
        raise RuntimeError("model down")


def test_the_model_writes_the_digest_from_the_facts_and_it_is_sent(migrated_db, fake_telegram):
    _a_day_of_checks(migrated_db)
    run_id = _digest_run(migrated_db)
    model = StubModel(replies=["  All quiet apart from one 503.  "], tokens_per_reply=321)

    run_digest(
        migrated_db,
        run_id,
        model,
        TelegramClient("1:a", fake_telegram.url),
        CHAT_ID,
        worker_id="worker-test",
        retry_backoff_seconds=0,
    )

    assert fake_telegram.sent() == [{"chat_id": CHAT_ID, "text": "All quiet apart from one 503."}]
    assert SITE in model.prompts[0] and "0.82" in model.prompts[0]
    status, tokens = migrated_db.execute(
        "SELECT status, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert (status, tokens) == ("succeeded", 321)
    kinds = [
        row[0]
        for row in migrated_db.execute(
            "SELECT payload->>'kind' FROM events WHERE run_id = %s ORDER BY seq", (run_id,)
        )
    ]
    assert kinds == ["gather", "write", "done"]
    written = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'write'", (run_id,)
    ).fetchone()[0]
    assert written["source"] == "model" and written["sent"] is True


def test_if_the_model_never_answers_the_plain_facts_are_sent(migrated_db, fake_telegram):
    _a_day_of_checks(migrated_db)
    run_id = _digest_run(migrated_db)

    run_digest(
        migrated_db,
        run_id,
        _DeadModel(),
        TelegramClient("1:a", fake_telegram.url),
        CHAT_ID,
        worker_id="worker-test",
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    [message] = fake_telegram.sent()
    assert message["text"] == render_facts(gather_facts(migrated_db))
    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("succeeded",)


def test_a_digest_with_no_chat_to_send_to_ends_in_error(migrated_db):
    run_id = _digest_run(migrated_db)

    run_digest(
        migrated_db,
        run_id,
        StubModel(replies=["hi"]),
        None,
        None,
        worker_id="worker-test",
        retry_backoff_seconds=0,
    )

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("error",)


def test_a_long_reply_is_cut_to_fit_one_telegram_message(migrated_db, fake_telegram):
    run_id = _digest_run(migrated_db)

    run_digest(
        migrated_db,
        run_id,
        StubModel(replies=["x" * 5000]),
        TelegramClient("1:a", fake_telegram.url),
        CHAT_ID,
        worker_id="worker-test",
    )

    assert len(fake_telegram.sent()[0]["text"]) <= 4000


def test_the_worker_runs_a_claimed_digest(migrated_db, fake_telegram):
    run_id = migrated_db.execute(
        "INSERT INTO runs (task, type) VALUES ('2026-10-01', 'digest') RETURNING id"
    ).fetchone()[0]
    claim_run(migrated_db, run_id, "worker-test")
    settings = settings_with(
        worker_id="worker-test", telegram_bot_token="123:abc", telegram_api_url=fake_telegram.url
    )

    process_run(
        migrated_db,
        run_id,
        settings,
        model_builder=stub_model_builder("Quiet day."),
        owner_chat_id=CHAT_ID,
    )

    assert fake_telegram.sent() == [{"chat_id": CHAT_ID, "text": "Quiet day."}]


def _at(hour: int, minute: int, day: int = 1) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=MELBOURNE)


def _schedule(conn, api, now):
    return schedule_digest(conn, api, BEARER_TOKEN, "Australia/Melbourne", now=now)


def test_the_digest_is_not_due_before_half_past_seven(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    api = start_server()

    assert _schedule(migrated_db, api, _at(7, 0)) is None


def test_the_first_tick_after_half_past_seven_creates_one_digest_a_day(
    start_server, migrated_db, monkeypatch
):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    api = start_server()

    run_id = _schedule(migrated_db, api, _at(8, 0))

    row = migrated_db.execute(
        "SELECT type, task, status FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("digest", "2026-10-01", "pending")
    assert _schedule(migrated_db, api, _at(9, 0)) is None
    assert _schedule(migrated_db, api, _at(23, 0)) is None


def test_the_next_day_gets_its_own_digest(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    api = start_server()
    first = _schedule(migrated_db, api, _at(8, 0))
    migrated_db.execute("UPDATE runs SET created_at = %s WHERE id = %s", (_at(8, 0), first))

    second = _schedule(migrated_db, api, _at(8, 0, day=2))

    assert second is not None and second != first
    task = migrated_db.execute("SELECT task FROM runs WHERE id = %s", (second,)).fetchone()[0]
    assert task == "2026-10-02"
