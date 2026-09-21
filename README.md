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
| A retried step that already committed is a no-op | `tests/test_idempotent.py` | pending |
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

## Cost

Target $0 a month idle: Container Apps free grant at scale to zero, Supabase free plan, GitHub free tier. The only variable cost is model tokens during a demo, capped per run and per day.

## Licence

MIT.
