"""The two caps above a run's own token budget, per docs/mercury.md: tokens
per day per provider, and dollars per month. A tripped cap ends the run
with one event and sends one Telegram message to the owner's chat."""

import pytest

from app.budget import BudgetTrip, check_budget
from app.tasks import TASK_TYPES
from app.worker import claim_next_run, process_run
from tests.test_worker import PASSING_TEST, settings_with, stub_model_builder

OWNER = 42


def spend(conn, provider: str, tokens: int, when: str = "now()") -> None:
    run_id = conn.execute(
        f"INSERT INTO runs (task, type, provider, status, finished_at, created_at) "
        f"VALUES ('x', 'pytest', %s, 'succeeded', {when}, {when}) RETURNING id",
        (provider,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO steps (run_id, seq, kind, tokens, finished_at) "
        f"VALUES (%s, 1, 'act', %s, {when})",
        (run_id, tokens),
    )


def new_run(conn, type_="pytest", provider="gemini", telegram=False) -> str:
    chat = (OWNER, 9) if telegram else (None, None)
    run_id = conn.execute(
        "INSERT INTO runs (task, type, provider, telegram_chat_id, telegram_message_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (PASSING_TEST, type_, provider, *chat),
    ).fetchone()[0]
    claim_next_run(conn, "worker-test")
    return str(run_id)


def no_model(settings, provider):
    raise AssertionError("a tripped cap must stop the run before a model is built")


def run(conn, run_id, fake_telegram, model_builder=no_model, **overrides):
    settings = settings_with(
        telegram_bot_token="123:abc", telegram_api_url=fake_telegram.url, **overrides
    )
    return process_run(conn, run_id, settings, model_builder=model_builder, owner_chat_id=OWNER)


def events(conn, run_id) -> list[dict]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT payload FROM events WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()
    ]


def test_a_provider_over_its_daily_tokens_trips(migrated_db):
    spend(migrated_db, "gemini", 400_000)
    spend(migrated_db, "gemini", 100_000)

    trip = check_budget(migrated_db, "gemini", daily_tokens=500_000, monthly_usd=5.0)

    assert trip == BudgetTrip(
        cap="daily_tokens", message="gemini has used 500,000 of its 500,000 tokens today."
    )


def test_under_the_daily_cap_nothing_trips(migrated_db):
    spend(migrated_db, "gemini", 499_999)

    assert check_budget(migrated_db, "gemini", daily_tokens=500_000, monthly_usd=5.0) is None


def test_yesterday_and_other_providers_do_not_count_today(migrated_db):
    spend(migrated_db, "gemini", 499_999, when="now() - interval '1 day'")
    spend(migrated_db, "haiku", 400_000)

    assert check_budget(migrated_db, "gemini", daily_tokens=500_000, monthly_usd=5.0) is None


def test_the_month_trips_on_dollars_across_providers(migrated_db):
    # Haiku at 5 USD per million: 1,000,000 tokens is 5.00 USD.
    spend(migrated_db, "haiku", 1_000_000, when="date_trunc('month', now())")

    trip = check_budget(migrated_db, "gemini", daily_tokens=10_000_000, monthly_usd=5.0)

    assert trip == BudgetTrip(
        cap="monthly_usd", message="This month's model spend is 5.00 of 5.00 USD."
    )


def test_free_tier_tokens_cost_nothing_toward_the_month(migrated_db):
    spend(migrated_db, "gemini", 9_000_000)

    assert check_budget(migrated_db, "haiku", daily_tokens=10_000_000, monthly_usd=5.0) is None


def test_a_tripped_run_ends_with_one_event_and_one_message(migrated_db, fake_telegram):
    spend(migrated_db, "gemini", 500_000)
    run_id = new_run(migrated_db)

    run(migrated_db, run_id, fake_telegram)

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
    assert status == "budget"
    [event] = events(migrated_db, run_id)
    assert event["kind"] == "done"
    assert event["output"]["status"] == "budget"
    assert event["output"]["cap"] == "daily_tokens"
    [message] = fake_telegram.sent()
    assert message["chat_id"] == OWNER
    assert message["text"] == (
        f"Budget: run {run_id[:8]} stopped. gemini has used 500,000 of its 500,000 tokens today."
    )


def test_a_chat_run_is_held_to_the_caps_too(migrated_db, fake_telegram):
    spend(migrated_db, TASK_TYPES["chat"].provider, 500_000)
    run_id = new_run(migrated_db, type_="chat", telegram=True)

    run(migrated_db, run_id, fake_telegram)

    assert migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone() == (
        "budget",
    )
    assert len(fake_telegram.sent()) == 1


def test_a_trip_with_no_owner_chat_still_ends_the_run(migrated_db, fake_telegram):
    spend(migrated_db, "gemini", 500_000)
    run_id = new_run(migrated_db)
    settings = settings_with(telegram_bot_token="123:abc", telegram_api_url=fake_telegram.url)

    process_run(migrated_db, run_id, settings, model_builder=no_model, owner_chat_id=None)

    assert migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone() == (
        "budget",
    )
    assert fake_telegram.sent() == []


def test_a_run_that_spends_its_own_budget_sends_one_message(migrated_db, fake_telegram):
    run_id = new_run(migrated_db)

    result = run(
        migrated_db,
        run_id,
        fake_telegram,
        model_builder=stub_model_builder("def add(a, b):\n    return a - b"),
        token_budget=150,
    )

    assert result.status == "budget_exhausted"
    [message] = fake_telegram.sent()
    assert message == {
        "chat_id": OWNER,
        "text": f"Budget: run {run_id[:8]} stopped at its own budget of 150 tokens.",
    }


@pytest.mark.parametrize(
    "name, value", [("DAILY_TOKENS_PER_PROVIDER", "1234"), ("MONTHLY_BUDGET_USD", "2.5")]
)
def test_the_caps_are_config(monkeypatch, name, value):
    from app.config import load_settings

    monkeypatch.setenv(name, value)
    settings = load_settings()

    assert (settings.daily_tokens_per_provider, settings.monthly_budget_usd) == {
        "DAILY_TOKENS_PER_PROVIDER": (1234, 5.0),
        "MONTHLY_BUDGET_USD": (500_000, 2.5),
    }[name]
