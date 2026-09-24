# agent-runs

A FastAPI agent loop that streams its steps over SSE and resumes from the last event after a dropped connection. Run state lives in PostgreSQL, so more than one replica serves the same run. Deployed to Azure Container Apps at scale to zero by GitHub Actions.

The service builds as one image, runs under `docker compose` against Postgres with pgvector, and answers `GET /health`. The design is in `docs/design.md`. The claims table below fills in as each test goes green and not before.

## Running it locally

```
docker compose up --build --wait
curl http://localhost:8000/health
```

That prints `{"status":"ok"}`. The same image serves both roles, chosen by the `ROLE` environment variable, which is `api` or `worker`. The worker arrives at task 3.

The tests need a Postgres to work against. They use `TEST_DATABASE_URL`, not `DATABASE_URL`, because they drop and recreate the public schema and `DATABASE_URL` points at a real project in a local `.env`. A non local host is refused unless you set `ALLOW_REMOTE_TEST_DB`.

The default is `agent_runs_test` on the compose Postgres, which the fixtures create, and deliberately not the `agent_runs` database the stack itself uses. A running worker polls that one, and it will claim a run a test has just created and write its own steps into it. The tests pass with the stack up because of that separation, not by luck.

```
docker compose up -d db --wait
uv sync
uv run ruff check .
uv run pytest
```

## The demo page

`static/index.html` is served at `/`. It posts a task, opens an `EventSource` on
the run's stream, and prints each event as it arrives. The kill connection
button calls `close()` on the `EventSource`. The browser's own reconnect logic
then reopens the connection with `Last-Event-ID` set to the last event id it
saw, so the resume protocol runs itself with no code in the page for it. One
static file, 48 lines, no build step. `docs/mercury.md` step 6a replaces it
with a React app, so nothing here is meant to last.

## Claims and their proof

| Claim | Test | Status |
|---|---|---|
| A client that drops mid run reconnects with `Last-Event-ID` and receives the remaining steps exactly once | `tests/test_resume.py` | green |
| Two replicas serve one run; a reconnect to the other replica resumes correctly | `tests/test_two_replicas.py` | green |
| A retried step that already committed is a no-op | `tests/test_idempotent.py` | green |
| Retrieval over the corpus feeds the loop | `tests/test_retrieval.py` | green, by full text search |
| Deployed to Azure Container Apps by GitHub Actions with OIDC | `.github/workflows/deploy.yml` publishes, `config/private-repo/deploy.yml` deploys | green |
| A run is one trace across the API and the worker, with the context carried on the run row | `tests/test_loop.py::test_the_loop_writes_step_spans_as_children_of_the_stored_trace_context` | green |
| A scheduled task runs with no client connected | `tests/test_scheduler.py`, and the live Job's log line and run row below | green |

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

### The same thing against a real run

Posting a task, dropping the connection part way through, and reconnecting. The
agent is Gemini 3.8 Flash on its free tier. Steps 1 to 3 arrive while the client
is connected, the client drops the socket, the verify step runs with nobody
listening, and the reconnect asks for everything after event 3.

```
run: 0d33b20c-1686-4827-a038-8af2724cbff7
[first pass] HTTP 200, Last-Event-ID sent: None
[first pass] event id=1 kind=plan seq=1
[first pass] event id=2 kind=retrieve seq=2
[first pass] event id=3 kind=act seq=3
[first pass] >>> connection dropped after 3 events

... steps continue on the server with nobody listening ...

[after reconnect] HTTP 200, Last-Event-ID sent: 3
[after reconnect] event id=4 kind=verify seq=4
[after reconnect] event id=5 kind=done seq=5
```

Events 4 and 5 arrive once each and nothing is repeated. The run finished
`succeeded` in one attempt, 167 tokens, 14.8 seconds, having written this from
the supplied pytest file alone:

```python
import re


def tokenise(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text)
```

### What a run costs

Measured on the two runs above, both solved on the first attempt: 167 and 325
tokens, 14.8 and 22.6 seconds. The per run budget is 50000 tokens, so the cap is
far above what the demo task needs. It exists for the case where the agent keeps
failing and retrying.

## Two replicas, one port

Compose runs two api replicas. Neither publishes a host port, so nginx is the
only way in and a client cannot pin itself to one of them. nginx re-resolves
Docker's DNS on every request, which is what spreads them.

```
$ for i in $(seq 8); do curl -sD- -o/dev/null localhost:8000/health | grep -i x-replica; done | sort | uniq -c
      6 x-replica: 13f2ed018fc0
      2 x-replica: 78634d84dabc
```

Every response names the replica that served it, so a test can show a reconnect
moved between processes rather than assuming it.

`tests/test_two_replicas.py` reads three events, drops the socket, writes three
more while nobody is connected, then reconnects with `Last-Event-ID` until the
proxy sends it to the other replica. It asserts that no event is repeated, none
is lost, and six arrive in total.

```
tests/test_two_replicas.py::test_a_stream_dropped_on_one_replica_resumes_on_the_other PASSED [ 50%]
tests/test_two_replicas.py::test_both_replicas_answer_through_the_one_port PASSED [100%]

======================= 2 passed, 56 deselected in 0.30s =======================
```

Run with one replica instead of two, both tests fail. Nothing about a connected
client lives in the api process, so there was no cursor to move. That is the
whole point of putting the events table in the middle.

These need the stack up, so they are marked `integration` and left out of the
default run.

```
docker compose up --build -d --wait
AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest -m integration
```

## A claim that outlives its worker

A worker killed outright cannot release its claim, so the claim carries a lease.
The worker refreshes `heartbeat_at` as it records each step, and a run whose
heartbeat has gone stale past `LEASE_SECONDS` can be taken over by another
worker. A run that already finished is never reclaimed.

The replacement worker continues from the last recorded seq rather than starting
again, so the steps the dead worker committed stay committed and its token spend
still counts against the budget. `tests/test_idempotent.py` and
`tests/test_loop.py` cover the takeover, the heartbeat, and the resume.

## What the agent does

You post a pytest file. The agent writes the function that makes it pass. Verification is pytest's exit code, run in a subprocess with a timeout, a scrubbed environment so no key reaches the generated code, and both of Python's socket layers disabled.

That is a demo guard, not isolation, and the difference matters. The worker container itself has a network, because it calls the model and Postgres. Disabling `socket` and `_socket` stops generated code reaching out by accident, and `tests/test_sandbox.py` asserts that both a plain `socket.create_connection` and a raw `import _socket` fail. It would not stop code written to get out.

The subprocess starts with the socket module replaced, so the code under verification cannot open a connection. `tests/test_sandbox.py` asserts that a test which calls `socket.create_connection` fails, that a solution which loops forever is killed at the timeout, and that a wrong answer comes back with pytest's output rather than an exception.

## The worker and the loop

The worker claims a run with one `UPDATE ... WHERE claimed_by IS NULL`, so two workers racing for the same run produce one winner and one no-op.

A model provider returning an error must not take the worker down with it. A failed call is retried with a growing wait, and a run that still cannot finish is closed with a `done` event carrying `status: error`, so a client waiting on the stream is not left hanging. This was not theoretical: the first live run against Gemini hit a 503, the worker had no handler, and the container died leaving the run claimed and its stream open forever. It then runs plan, retrieve, act, verify, and repeats until the verifier passes or the token budget is gone. Retrieval is a stub until task 5.

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

## Retrieval

The corpus is 35 chunks of Python standard library notes in `corpus/`, indexed
into the `chunks` table when the worker starts. Indexing is keyed on
`(source, ord)`, so restarting the worker adds nothing.

There are two retrieval paths behind one interface. With `VOYAGE_API_KEY` set,
chunks carry embeddings and the search is cosine distance in pgvector. Without
it, the search is Postgres full text search over the same rows. This README says
retrieval, not vector, because the numbers below came from the full text path.
The vector path is tested against real pgvector with a deterministic offline
embedder, so the SQL is proven even without a key.

Asking for the chunk behind a regular expression task returns this:

```
python_stdlib  ->  re.split(pattern, string) splits on a pattern rather than a fixed separator...
python_stdlib  ->  unittest.mock is rarely the answer in tests. Prefer passing a real object...
python_stdlib  ->  re.findall(pattern, string) returns every non overlapping match as a list...
```

```
tests/test_retrieval.py::test_indexing_the_corpus_stores_every_chunk PASSED [  6%]
tests/test_retrieval.py::test_indexing_twice_adds_nothing PASSED         [ 13%]
tests/test_retrieval.py::test_full_text_search_returns_the_chunk_that_matches PASSED [ 20%]
tests/test_retrieval.py::test_full_text_search_ranks_the_best_chunk_first PASSED [ 26%]
tests/test_retrieval.py::test_full_text_search_returns_nothing_for_an_unrelated_query PASSED [ 33%]
tests/test_retrieval.py::test_vector_search_returns_the_nearest_chunk PASSED [ 40%]
tests/test_retrieval.py::test_vector_search_stores_an_embedding_for_every_chunk PASSED [ 46%]
tests/test_retrieval.py::test_text_search_needs_no_embeddings PASSED     [ 53%]
tests/test_retrieval.py::test_without_an_embedding_key_retrieval_is_full_text_search PASSED [ 60%]
tests/test_retrieval.py::test_with_an_embedding_key_retrieval_is_vector_distance PASSED [ 66%]

============================== 15 passed in 3.37s ==============================
```

Two things about the corpus are worth saying plainly. Postgres full text search
joins every term of a query with AND by default, which means a chunk only matches
when it contains all of them, and a pytest file never does. The query is rewritten
to match any term and rank the result. Second, the repo's own README and design
docs were in the corpus first, as the spec asked. They crowded the standard
library notes out, because they are full of the same words a pytest file uses
while saying nothing about how to write Python. The corpus is reference material
only now, and the spec records why.

## Live on Azure

Deployed by GitHub Actions with an OIDC federated credential, so there is no
stored cloud secret. This repo's `deploy.yml` runs the two replica test against
compose and pushes the image to GHCR tagged with its commit, and deploys
nothing. The private config repo's workflow checks this repo out at a commit it
pins by hand, runs `infra/deploy.sh` with that commit's image and its own
`mercury.yaml`, and polls the live health endpoint through a cold start. A push
here changes nothing live until that pin moves.

A run posted to the deployed service, streamed over SSE from Container Apps:

```
--- plan (seq 1)
--- retrieve (seq 2)
--- act (seq 3) ---
from functools import reduce


def initials(name: str) -> str:
    """Return the initials of a name in the format 'X.Y.'."""
    words = name.split()
    return reduce(lambda acc, word: acc + f"{word[0].upper()}.", words, "")
--- verify (seq 4): passed=True timed_out=False
=== done: succeeded after 1 attempt(s) ===
```

That run finished in one attempt on 223 tokens. The retrieve step had returned
this chunk, which is why the answer reaches for `functools.reduce` rather than a
loop:

```
functools.reduce folds a binary function over an iterable, with an optional initial value.
```

Both apps scale to zero. The worker starts when there is an unfinished run and
stops again afterwards. Nothing runs, and nothing is billed, between demos.

### What the ingress does to a long stream

Container Apps cuts any HTTP request at 240 seconds on the consumption plan.
Keepalives do not extend it, and raising it needs premium ingress, which is paid
and therefore out of scope here. A run that takes longer than that will have its
stream cut by the platform, and the client has to reconnect with `Last-Event-ID`
and carry on, which is exactly the path this repo exists to make work. The
reconnect is free because the events table is the cursor.

## A scheduled run with nobody watching

A Container Apps Job named `agent-runs-scheduler` wakes on the hour, reads the
site list from `mercury.yaml`, and posts a `site_check` run to the API with the
bearer token. It then checks the site with a plain HTTP request in the same
process, writes the result as the run's one step, and closes the run. No model
is called and no browser is connected. The Job exits when it is done, so it
costs nothing between runs, and the API and worker stay at zero replicas until
it wakes them. The worker never claims a `site_check` run, because it would
otherwise race the scheduler for it and count it against the daily limit.

The real `mercury.yaml` lives in a private repo, whose workflow passes it to the
Bicep when it deploys. A deploy without it gives the Job a placeholder listing
no sites.

The Job's own log line, exactly as Log Analytics holds it for container
`agent-runs-scheduler`, from an execution started with `az containerapp job start`:

```
{"timestamp": "2026-09-23T10:16:47.614108+00:00", "level": "INFO", "logger": "agent_runs.scheduler", "message": "scheduled run a9f4b60f-4ff1-4f6f-872e-cbb61a216c62 created with no client", "run_id": "a9f4b60f-4ff1-4f6f-872e-cbb61a216c62"}
```

The same run from the live `GET /runs`, closed in 84 ms with 0 tokens:

```
{
    "id": "a9f4b60f-4ff1-4f6f-872e-cbb61a216c62",
    "type": "site_check",
    "provider": null,
    "executor": null,
    "status": "succeeded",
    "tokens": 0,
    "duration_seconds": 0.083938,
    "created_at": "2026-09-23T10:16:47.591056+00:00"
}
```

A site that fails three checks in a row is suspended until something resumes
it. A POST the API refuses counts as a failure too, so a wrong or expired token
suspends the schedule rather than skipping every site each hour with nothing to
show for it.

```
tests/test_scheduler.py::test_run_due_checks_creates_a_run_and_writes_its_result PASSED [ 16%]
tests/test_scheduler.py::test_run_due_checks_records_a_failed_check PASSED [ 33%]
tests/test_scheduler.py::test_run_due_checks_skips_a_suspended_site PASSED [ 50%]
tests/test_scheduler.py::test_run_due_checks_suspends_after_the_third_consecutive_failure PASSED [ 66%]
tests/test_scheduler.py::test_run_due_checks_counts_a_refused_post_as_a_failure PASSED [ 83%]
tests/test_scheduler.py::test_run_due_checks_logs_the_created_run_with_no_client PASSED [100%]

============================== 6 passed in 4.70s ===============================
```

## Observability

A run is one trace across both roles, not two unrelated ones. `POST /runs` opens a
root span and stores its W3C `traceparent` on the run row. The worker restores that
context as current before the loop starts, not just once per step, so its five step
spans, plan, retrieve, act, verify, done, each carrying the step kind, the provider,
and its tokens, land as children of the api's root span, and so does a log line from
anywhere in the loop, including a failure that writes no step. A run whose lease
went stale and was picked up by a replacement worker gets one more child span,
marked as a takeover, so the trace shows where the first worker's spans stop and the
second's begin. `OTEL_SERVICE_NAME` is set per role in the Bicep, so Application
Insights tells the api and the worker apart by `cloud_RoleName` instead of showing
both as `unknown_service`.

```
tests/test_telemetry.py::test_instrumenting_at_construction_puts_otel_middleware_in_the_stack PASSED [ 25%]
tests/test_loop.py::test_the_loop_writes_step_spans_as_children_of_the_stored_trace_context PASSED [ 50%]
tests/test_loop.py::test_a_resumed_run_gets_a_takeover_span_in_the_stored_trace PASSED [ 75%]
tests/test_loop.py::test_a_failure_log_line_inside_the_loop_carries_the_run_s_trace_id PASSED [100%]

============================== 4 passed in 3.78s ===============================
```

Checked against the live deployment, not just the unit tests: one run's `operation_Id`
in Application Insights covers the api's root span and every one of the worker's step
spans through to `step.done`, with `cloud_RoleName` telling the two roles apart.

```
$ az rest --method post \
    --url "https://api.applicationinsights.io/v1/apps/154a7510-caa6-4649-b858-ba28d5b21abd/query" \
    --resource "https://api.applicationinsights.io" \
    --body '{"query":"dependencies | where timestamp > ago(15m) | where name startswith \"step.\" or name == \"run\" | project name, operation_Id, cloud_RoleName | order by timestamp asc"}'

name          operation_Id                      cloud_RoleName
run           8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-api
step.plan     8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-worker
step.retrieve 8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-worker
step.act      8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-worker
step.verify   8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-worker
...
step.done     8efea36baffe19ceb8e2e1f1554a9cd9  agent-runs-worker
```

The first test is the regression that survived one whole session undetected:
`FastAPIInstrumentor.instrument_app` works by patching `build_middleware_stack`, and
Starlette calls that method itself on the first ASGI scope the app receives,
including the lifespan startup scope, which runs before the lifespan function's own
body does. Instrumenting from inside the lifespan therefore patches a method that has
already been called, and nothing raises to say so. `create_app()` instruments before
returning the app, which is before uvicorn sends it anything.

Every process also writes structured JSON to stdout, which the managed environment
ships to the workspace on its own, not through the telemetry exporter, so logs keep
working in CI, in compose, and on a machine with no exporter configured. A line
carries a timestamp, level, logger, message, and whatever the call site passed as
`extra`; the trace and span ids come from whatever span is current when the line is
emitted. Neither a task's input body nor a model's output is ever logged.

```
{"timestamp": "2026-09-22T10:43:45.370354+00:00", "level": "INFO", "logger": "agent_runs.worker", "message": "claimed run 9af98c5f-ac20-4a35-8cde-5f9e3e1641f2", "run_id": "9af98c5f-ac20-4a35-8cde-5f9e3e1641f2", "worker_id": "413aae328332-f84f09b2"}
```

## A note on free tier quotas

`MAX_RUNS_PER_DAY` caps runs, not model calls, and one run makes up to ten calls
while it retries. Gemini's free tier allows 20 calls a day on `gemini-3.8-flash`,
so a handful of failing runs exhausts it. When that happens the run is closed
with `status: error` rather than hanging, which is what the error path is for.
`gemini-3.5-flash-lite` has a larger free allowance.

## Guards

Three numbers are configuration, not code. `TOKEN_BUDGET` is 50000 per run and is counted in the loop. `MAX_RUNS_PER_DAY` is 20 and is counted in the worker from `runs.created_at`, and a refused run still gets its `done` event so a client waiting on the stream is not left hanging. `MODEL=stub` runs the loop with an offline model that needs no key, which is what CI uses so a push costs nothing.

The loop talks to a `Model` protocol with one `complete` method. As of Mercury step 1 the provider is no longer read from `MODEL`: each task type in `app/tasks.py` names a provider, and `PROVIDERS` in `app/config.py` maps that name to a kind, a base URL, a model, and the env var holding its key. `pytest` names `gemini`, whose key is `MODEL_API_KEY`; the registry also carries `haiku`, whose key is `ANTHROPIC_API_KEY`, for the task types Mercury adds later. A run whose provider has no key configured is refused, the same way a run past the daily limit is, so its stream closes with `status: refused` rather than staying claimed forever. `.env.example` lists both key variables.

## Cost

Target $0 a month idle: Container Apps free grant at scale to zero, Supabase free plan, GitHub free tier. The only variable cost is model tokens during a demo, capped per run and per day.

## Licence

MIT.
