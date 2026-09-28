"""Progress on Telegram: one message per run, edited in place as its steps
land, per docs/mercury.md. Only a run asked for from Telegram has one."""

import httpx2

from app.progress import push_progress, render_progress
from app.runs import record_step
from app.worker import claim_next_run, process_run
from tests.test_worker import CORRECT, PASSING_TEST, settings_with, stub_model_builder

CHAT, MESSAGE = 42, 7


def insert(conn, type_="pytest", task=PASSING_TEST, telegram=True, **columns) -> str:
    names = ["task", "type", *columns]
    values = [task, type_, *columns.values()]
    if telegram:
        names += ["telegram_chat_id", "telegram_message_id"]
        values += [CHAT, MESSAGE]
    sql = (
        f"INSERT INTO runs ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) "
        "RETURNING id"
    )
    return str(conn.execute(sql, values).fetchone()[0])


def edits(fake_telegram) -> list[str]:
    return [p["text"] for p in fake_telegram.sent("editMessageText")]


def telegram_settings(fake_telegram, **overrides):
    return settings_with(
        telegram_bot_token="123:abc", telegram_api_url=fake_telegram.url, **overrides
    )


def test_render_shows_each_step_and_never_the_code_or_the_test_output(migrated_db):
    run_id = insert(migrated_db)
    record_step(migrated_db, run_id, 1, "plan", output={"text": "write solution.py"})
    record_step(migrated_db, run_id, 2, "retrieve", output={"chunks": [{"id": 1}, {"id": 2}]})
    record_step(migrated_db, run_id, 3, "act", output={"code": "def add(a, b): SECRET_CODE"})
    record_step(
        migrated_db,
        run_id,
        4,
        "verify",
        output={"passed": False, "timed_out": False, "output": "SECRET_OUTPUT"},
    )

    text = render_progress(migrated_db, run_id)

    assert text.splitlines()[0] == f"pytest run {run_id[:8]}: pending"
    assert text.splitlines()[1:] == [
        "1 plan",
        "2 retrieved 2 notes",
        "3 wrote solution.py",
        "4 pytest failed",
    ]
    assert "SECRET" not in text


def test_render_summarises_a_lighthouse_and_a_crawl_result(migrated_db):
    lighthouse = insert(migrated_db, "site_check", "https://a.example/", check_kind="lighthouse")
    record_step(
        migrated_db,
        lighthouse,
        1,
        "check",
        output={"scores": {"performance": 0.97, "accessibility": 1}, "lcp_ms": 2275},
    )
    crawl = insert(migrated_db, "site_check", "https://a.example/", check_kind="broken_links")
    record_step(
        migrated_db,
        crawl,
        1,
        "check",
        output={"pages_checked": 12, "broken_count": 2, "broken": []},
    )

    assert "1 performance 0.97, accessibility 1, LCP 2275 ms" in render_progress(
        migrated_db, lighthouse
    )
    assert "1 12 pages, 2 broken links" in render_progress(migrated_db, crawl)


def test_a_worker_run_edits_its_one_message_after_every_step(migrated_db, fake_telegram):
    run_id = insert(migrated_db)
    claim_next_run(migrated_db, "worker-test")

    process_run(
        migrated_db,
        run_id,
        telegram_settings(fake_telegram),
        model_builder=stub_model_builder(CORRECT),
    )

    sent = fake_telegram.sent("editMessageText")
    assert {(p["chat_id"], p["message_id"]) for p in sent} == {(CHAT, MESSAGE)}
    assert len(sent) >= 5
    last = edits(fake_telegram)[-1]
    assert last.splitlines()[0].startswith(f"pytest run {run_id[:8]}: succeeded")
    assert last.splitlines()[-1] == "5 done, succeeded"
    assert fake_telegram.sent() == []


def test_a_run_nobody_asked_for_on_telegram_sends_nothing(migrated_db, fake_telegram):
    run_id = insert(migrated_db, telegram=False)
    claim_next_run(migrated_db, "worker-test")

    process_run(
        migrated_db, run_id, telegram_settings(fake_telegram), model_builder=stub_model_builder()
    )

    assert fake_telegram.calls == []


def test_a_refused_run_says_why(migrated_db, fake_telegram):
    run_id = insert(migrated_db)
    claim_next_run(migrated_db, "worker-test")

    process_run(
        migrated_db,
        run_id,
        telegram_settings(fake_telegram, max_runs_per_day=0),
        model_builder=stub_model_builder(),
    )

    assert "daily limit of 0 runs reached" in edits(fake_telegram)[-1]
    assert edits(fake_telegram)[-1].startswith(f"pytest run {run_id[:8]}: refused")


def test_a_telegram_failure_does_not_stop_the_run(migrated_db, fake_telegram):
    run_id = insert(migrated_db)
    claim_next_run(migrated_db, "worker-test")
    fake_telegram.fail_next = {
        "error_code": 400,
        "description": "Bad Request: message to edit not found",
    }

    result = process_run(
        migrated_db, run_id, telegram_settings(fake_telegram), model_builder=stub_model_builder()
    )

    assert result.status == "succeeded"


def test_push_progress_with_no_client_does_nothing(migrated_db):
    run_id = insert(migrated_db)

    push_progress(migrated_db, run_id, None)


def test_a_self_hosted_check_result_edits_the_message(
    start_server, migrated_db, fake_telegram, monkeypatch
):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    base_url = start_server()
    run_id = insert(migrated_db, "site_check", "https://a.example/", check_kind="broken_links")
    auth = {"Authorization": "Bearer t"}
    claimed = httpx2.post(
        f"{base_url}/checks/claim", json={"worker_id": "w", "kinds": ["broken_links"]}, headers=auth
    )
    assert claimed.json()["id"] == run_id

    httpx2.post(
        f"{base_url}/checks/{run_id}/result",
        json={"worker_id": "w", "result": {"pages_checked": 3, "broken_count": 0, "broken": []}},
        headers=auth,
    )

    last = edits(fake_telegram)[-1]
    assert last.splitlines()[0].startswith(f"site_check run {run_id[:8]}: succeeded")
    assert "1 3 pages, 0 broken links" in last
