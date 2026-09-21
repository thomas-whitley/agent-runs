"""plan, retrieve, act, verify, repeat until the verifier passes or the budget is gone."""

from app.loop import run_agent_loop
from app.model import StubModel

PASSING_TEST = """
from solution import add

def test_add():
    assert add(2, 3) == 5
"""

CORRECT = "Here you go.\n```python\ndef add(a, b):\n    return a + b\n```"
WRONG = "Here you go.\n```python\ndef add(a, b):\n    return a * b\n```"


def new_run(conn, task: str) -> str:
    return conn.execute("INSERT INTO runs (task) VALUES (%s) RETURNING id", (task,)).fetchone()[0]


def event_kinds(conn, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT payload ->> 'kind' FROM events WHERE run_id = %s ORDER BY seq", (run_id,)
    ).fetchall()
    return [row[0] for row in rows]


def test_a_correct_first_answer_finishes_the_run(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)

    result = run_agent_loop(migrated_db, run_id, StubModel(replies=[CORRECT]), token_budget=50_000)

    assert result.status == "succeeded"
    assert result.attempts == 1
    assert event_kinds(migrated_db, run_id) == ["plan", "retrieve", "act", "verify", "done"]

    row = migrated_db.execute(
        "SELECT status, tokens_used, finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("succeeded", result.tokens_used, True)
    assert result.tokens_used > 0


def test_a_wrong_answer_is_retried_with_the_failure_fed_back(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    model = StubModel(replies=[WRONG, CORRECT])

    result = run_agent_loop(migrated_db, run_id, model, token_budget=50_000)

    assert result.status == "succeeded"
    assert result.attempts == 2
    assert "assert" in model.prompts[-1], "the verifier output was not fed back into the prompt"

    kinds = event_kinds(migrated_db, run_id)
    assert kinds.count("act") == 2
    assert kinds.count("verify") == 2
    assert kinds[-1] == "done"


def test_the_loop_stops_when_the_token_budget_is_spent(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    model = StubModel(replies=[WRONG], tokens_per_reply=400)

    result = run_agent_loop(migrated_db, run_id, model, token_budget=900)

    assert result.status == "budget_exhausted"
    assert result.attempts == 3
    assert result.tokens_used == 1200
    assert event_kinds(migrated_db, run_id)[-1] == "done"

    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
    assert status == "budget_exhausted"


def test_the_done_event_carries_the_final_status(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)

    run_agent_loop(migrated_db, run_id, StubModel(replies=[CORRECT]), token_budget=50_000)

    payload = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s ORDER BY seq DESC LIMIT 1", (run_id,)
    ).fetchone()[0]
    assert payload["kind"] == "done"
    assert payload["output"]["status"] == "succeeded"


def test_the_verify_step_records_whether_it_passed(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)

    run_agent_loop(migrated_db, run_id, StubModel(replies=[CORRECT]), token_budget=50_000)

    output = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'verify'", (run_id,)
    ).fetchone()[0]
    assert output["passed"] is True


class FlakyModel:
    """Raises a transient error a fixed number of times, then answers."""

    def __init__(self, failures: int, reply: str = CORRECT):
        self.failures = failures
        self.reply = reply
        self.calls = 0

    def complete(self, system: str, prompt: str):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("503 the model is experiencing high demand")
        from app.model import ModelReply

        return ModelReply(text=self.reply, tokens=100)


class AlwaysFailingModel:
    def __init__(self):
        self.calls = 0

    def complete(self, system: str, prompt: str):
        self.calls += 1
        raise RuntimeError("503 the model is experiencing high demand")


def test_a_transient_model_error_is_retried(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    model = FlakyModel(failures=2)

    result = run_agent_loop(
        migrated_db, run_id, model, token_budget=50_000, model_retry_backoff_seconds=0
    )

    assert result.status == "succeeded"
    assert model.calls == 3, "the call should have been retried twice before succeeding"


def test_a_model_that_keeps_failing_ends_the_run_instead_of_killing_the_worker(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    model = AlwaysFailingModel()

    result = run_agent_loop(
        migrated_db, run_id, model, token_budget=50_000, model_retry_backoff_seconds=0
    )

    assert result.status == "error"

    row = migrated_db.execute(
        "SELECT status, finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("error", True), "a run that errored must not be left claimed and running"

    payload = migrated_db.execute(
        "SELECT payload FROM events WHERE run_id = %s ORDER BY seq DESC LIMIT 1", (run_id,)
    ).fetchone()[0]
    assert payload["kind"] == "done", "an errored run must still close its stream"
    assert payload["output"]["status"] == "error"
    assert "high demand" in payload["output"]["error"]


def test_a_failing_model_is_not_retried_forever(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    model = AlwaysFailingModel()

    run_agent_loop(
        migrated_db,
        run_id,
        model,
        token_budget=50_000,
        model_retry_attempts=3,
        model_retry_backoff_seconds=0,
    )

    assert model.calls == 3


def test_the_loop_resumes_after_a_worker_died_part_way(migrated_db):
    """A replacement worker continues the run instead of starting it again."""
    from app.runs import record_step

    run_id = new_run(migrated_db, PASSING_TEST)
    record_step(migrated_db, run_id, 1, "plan", output={"text": "a plan"})
    record_step(migrated_db, run_id, 2, "retrieve", output={"chunks": []})
    record_step(migrated_db, run_id, 3, "act", output={"code": "broken"}, tokens=100)
    migrated_db.execute("UPDATE runs SET tokens_used = 100 WHERE id = %s", (run_id,))

    result = run_agent_loop(migrated_db, run_id, StubModel(replies=[CORRECT]), token_budget=50_000)

    assert result.status == "succeeded"
    assert event_kinds(migrated_db, run_id) == [
        "plan",
        "retrieve",
        "act",
        "act",
        "verify",
        "done",
    ]
    assert result.attempts == 2, "the attempt the dead worker made must still count"
    assert result.tokens_used == 200, "tokens already spent must carry over"

    seqs = [
        row[0]
        for row in migrated_db.execute(
            "SELECT seq FROM events WHERE run_id = %s ORDER BY seq", (run_id,)
        ).fetchall()
    ]
    assert seqs == [1, 2, 3, 4, 5, 6]


def test_the_loop_reports_progress_so_its_claim_stays_alive(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)
    beats = []

    run_agent_loop(
        migrated_db,
        run_id,
        StubModel(replies=[CORRECT]),
        token_budget=50_000,
        on_step=lambda: beats.append(1),
    )

    assert len(beats) >= 3, "the loop must report progress as it goes"


RETRIEVAL_CORPUS = [
    "The solution module should add two integers and return their sum.",
    "The datetime module supplies classes for manipulating dates and times.",
    "The heapq module turns a list into a min heap in place.",
]


def index_test_corpus(conn):
    from app.retrieval import CorpusChunk, index_corpus

    index_corpus(
        conn,
        [CorpusChunk(source="notes", ord=i, body=b) for i, b in enumerate(RETRIEVAL_CORPUS)],
    )


def test_the_retrieve_step_records_the_chunks_it_found(migrated_db):
    from app.retrieval import TextRetriever

    index_test_corpus(migrated_db)
    run_id = new_run(migrated_db, PASSING_TEST)

    run_agent_loop(
        migrated_db,
        run_id,
        StubModel(replies=[CORRECT]),
        token_budget=50_000,
        retriever=TextRetriever(),
    )

    output = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'retrieve'", (run_id,)
    ).fetchone()[0]

    assert output["chunks"], "retrieval found nothing for a task the corpus covers"
    assert output["chunks"][0]["source"] == "notes"


def test_the_retrieved_text_reaches_the_prompt(migrated_db):
    from app.retrieval import TextRetriever

    index_test_corpus(migrated_db)
    run_id = new_run(migrated_db, PASSING_TEST)
    model = StubModel(replies=[CORRECT])

    run_agent_loop(migrated_db, run_id, model, token_budget=50_000, retriever=TextRetriever())

    assert "add two integers" in model.prompts[0], "the chunk never reached the model"


def test_the_loop_still_runs_without_a_retriever(migrated_db):
    run_id = new_run(migrated_db, PASSING_TEST)

    result = run_agent_loop(migrated_db, run_id, StubModel(replies=[CORRECT]), token_budget=50_000)

    assert result.status == "succeeded"
