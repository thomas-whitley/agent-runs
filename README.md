# agent-runs

A FastAPI agent loop that streams its steps over SSE and resumes from the last event after a dropped connection. Run state lives in PostgreSQL, so more than one replica serves the same run. Deployed to Azure Container Apps at scale to zero by GitHub Actions.

Status: task 1 of the build order is done. The service builds as one image, runs under `docker compose` against Postgres with pgvector, and answers `GET /health`. The design is in `docs/spec.md` and the build order in `docs/build-brief.md`. The claims table below fills in as each test goes green and not before.

## Running it locally

```
docker compose up --build --wait
curl http://localhost:8000/health
```

That prints `{"status":"ok"}`. The same image serves both roles, chosen by the `ROLE` environment variable, which is `api` or `worker`. The worker arrives at task 3.

The tests need a Postgres to work against. They use `TEST_DATABASE_URL`, not `DATABASE_URL`, because they drop and recreate the public schema and `DATABASE_URL` points at a real project in a local `.env`. The default is the compose database, and a non local host is refused unless you set `ALLOW_REMOTE_TEST_DB`.

```
docker compose up -d db --wait
uv sync
uv run ruff check .
uv run pytest
```

## Claims and their proof

| Claim | Test | Status |
|---|---|---|
| A client that drops mid run reconnects with `Last-Event-ID` and receives the remaining steps exactly once | `tests/test_resume.py` | green |
| Two replicas serve one run; a reconnect to the other replica resumes correctly | `tests/test_two_replicas.py` | pending |
| A retried step that already committed is a no-op | `tests/test_idempotent.py` | green |
| Retrieval over the corpus feeds the loop | `tests/test_retrieval.py` | pending |
| Deployed to Azure Container Apps by GitHub Actions with OIDC, traced end to end | `.github/workflows/deploy.yml` | pending |

## Resume, and the test that proves it

The test opens a stream, lets steps 1 to 3 arrive while it is connected, drops the
connection, lets steps 4 and 5 happen with nobody listening, then reconnects with
`Last-Event-ID` set to the last event it saw. It asserts that no event is repeated,
none is lost, and six events are delivered in total.

```
tests/test_resume.py::test_stream_replays_every_event_then_closes_on_done PASSED [ 33%]
tests/test_resume.py::test_dropping_a_live_stream_and_reconnecting_resumes_exactly_once PASSED [ 66%]
tests/test_resume.py::test_unknown_run_is_not_found PASSED               [100%]

============================== 3 passed in 2.23s ===============================
```

The tests run against a real uvicorn on a real socket rather than an in process test
client, because an in process client buffers the response instead of delivering it
chunk by chunk and so cannot drop a live stream part way through.

The same thing by hand, against the compose stack, replaying from event 1:

```
$ curl -s -N -H "Last-Event-ID: 1" localhost:8000/runs/$RUN/events
id: 2
event: done
data: {"kind": "done", "status": "succeeded"}
```

Nothing about a connected client is held in the server. The events table is the
whole cursor, which is what lets a reconnect land on any replica.

## What the agent does

You post a pytest file. The agent writes the function that makes it pass, running pytest in a subprocess with a timeout and no network inside the worker container. That is the whole sandbox, and it is a demo sandbox, not a security boundary.

The subprocess starts with the socket module replaced, so the code under verification cannot open a connection. `tests/test_sandbox.py` asserts that a test which calls `socket.create_connection` fails, that a solution which loops forever is killed at the timeout, and that a wrong answer comes back with pytest's output rather than an exception.

## The worker and the loop

The worker claims a run with one `UPDATE ... WHERE claimed_by IS NULL`, so two workers racing for the same run produce one winner and one no-op. It then runs plan, retrieve, act, verify, and repeats until the verifier passes or the token budget is gone. Retrieval is a stub until task 5.

Each step writes one `steps` row and one `events` row in a single transaction, both keyed on `(run_id, seq)` with `ON CONFLICT DO NOTHING`. A worker that dies and is replaced repeats the same seq and writes nothing twice.

```
tests/test_idempotent.py::test_only_one_worker_claims_a_run PASSED       [ 11%]
tests/test_idempotent.py::test_a_repeated_step_writes_one_row_and_one_event PASSED [ 22%]
tests/test_idempotent.py::test_a_step_and_its_event_are_written_together PASSED [ 33%]
tests/test_idempotent.py::test_a_later_step_still_writes_after_a_retried_one PASSED [ 44%]
tests/test_sandbox.py::test_a_correct_solution_passes PASSED             [ 55%]
tests/test_sandbox.py::test_a_wrong_solution_fails_and_the_output_explains_why PASSED [ 66%]
tests/test_sandbox.py::test_a_solution_that_does_not_import_fails_rather_than_raising PASSED [ 77%]
tests/test_sandbox.py::test_a_solution_that_hangs_is_killed_at_the_timeout PASSED [ 88%]
tests/test_sandbox.py::test_the_sandbox_cannot_open_a_socket PASSED      [100%]

============================== 9 passed in 4.83s ===============================
```

## Guards

Three numbers are configuration, not code. `TOKEN_BUDGET` is 50000 per run and is counted in the loop. `MAX_RUNS_PER_DAY` is 20 and is counted in the worker from `runs.created_at`, and a refused run still gets its `done` event so a client waiting on the stream is not left hanging. `MODEL` is `claude-haiku-4-5-20251001`, and setting it to `stub` runs the loop with an offline model that needs no key, which is what CI uses so a push costs nothing.

## Cost

Target $0 a month idle: Container Apps free grant at scale to zero, Supabase free plan, GitHub free tier. The only variable cost is model tokens during a demo, capped per run and per day.

## Licence

MIT.
