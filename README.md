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

## The page at `/`

The API serves a built page from `web/dist` at `/`. It is registered after every API route, so `/health`, `/runs`, `/runs/{id}/events` and `/checks/*` never reach it, and any other GET gets the page. `tests/test_web_page.py` asserts both sides of that. It is a Vite, React and TypeScript app in `web/` that lists every run newest first, 50 at a time, with its type, provider, executor, status, tokens and duration, and an Older runs button that follows the endpoint's keyset cursor, which `web/test/runs-list.test.tsx` tests at the page boundary.

Each run id links to `#/runs/<id>`, which streams that run's events as they land. A Kill connection button closes the stream the way a dropped network would, and Reconnect opens a new one from the last event seen, with a marker row where the drop happened. The browser holds no token, so the page gets step numbers, kinds and the final status, and never the code, test output or diffs inside them, which need the bearer token. A browser `EventSource` cannot set `Last-Event-ID` on a new connection, so the page resumes with `?after=<id>`, and the header wins when both are sent. `useRunStream` merges events by id, so a row the server sends twice shows once, and it closes the stream on `done` so the browser does not keep reconnecting. `web/test/run-stream.test.tsx` covers the merge on reconnect, the duplicate id and the close on done against a fake `EventSource`, and `tests/test_events_without_token.py` covers the API side.

On the live deploy on 2026-10-01, run `ff9e0edd` was opened on the page and the connection killed after event #235, the retrieve step. The worker carried on with nobody listening. Reconnect, 62.3 seconds later, asked for everything after #235 and got #236 act, #237 verify and #238 done, once each, ending `succeeded`, with the drop marked between #235 and #236.

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
| A weekly check runs on a self hosted worker and falls back to the cloud path when it is offline | `tests/test_checks_integration.py` in CI's compose job, and `tests/test_scheduler.py` for the weekly schedule | green |
| A budget trip ends a run with one event and one message | `tests/test_budget.py`, against the fake Telegram in `tests/telegram_fake.py` | green |
| The webhook cold start is measured and stated | `tests/test_telegram_webhook.py::test_each_answer_logs_how_long_after_the_message_was_sent`, and the live log line below | green, 19.0 s from zero replicas |
| A Telegram message opens a PR on a named repo | `tests/test_repo_chore_github.py`, against the real `thomas-whitley/mercury-fixture` | green when run with a GitHub token, output below. CI has no token, so it skips there |
| A dropped browser stream resumes from the last event without duplicating rows | `web/test/run-stream.test.tsx` against a fake `EventSource`, `tests/test_events_without_token.py`, and the live page below | green |

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

The same thing by hand, against the compose stack started with `MERCURY_BEARER_TOKEN` exported, replaying from event 1. A pytest run's events sit behind the bearer token, for the reason under "What the agent does".

```
$ curl -s -N -H "Authorization: Bearer $MERCURY_BEARER_TOKEN" -H "Last-Event-ID: 1" localhost:8000/runs/$RUN/events
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
default run. The run they create needs the bearer token, so the stack and the
test share one.

```
export MERCURY_BEARER_TOKEN=$(openssl rand -hex 32)
docker compose up --build -d --wait
AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest tests/test_two_replicas.py -m integration
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

You post a pytest file with the bearer token. The agent writes the function that makes it pass. Verification is pytest's exit code, run in a subprocess with a timeout, a scrubbed environment, and both of Python's socket layers disabled.

That is a demo guard, not isolation, and the difference matters. The worker container itself has a network, because it calls the model and Postgres. Disabling `socket` and `_socket` stops generated code reaching out by accident, and `tests/test_sandbox.py` asserts that both a plain `socket.create_connection` and a raw `import _socket` fail. It would not stop code written to get out.

The scrubbed environment keeps keys out of the subprocess's own environment and no further. A subprocess running as the worker's user can still read the worker's environment from `/proc`, database URL and model keys included, and print it into the verify output. In the image the worker is root, so pytest runs as a separate `sandbox` user instead, and a worker that is root with no such user refuses to run the file at all. Checked in the built image on 2026-09-30 with a fake key in the worker's environment, a posted test that reads `/proc/1/environ` ran as uid 998 and got `PermissionError`. The same test against the previous image ran as uid 0 and printed the key.

The sandbox user closes that one path. The file still runs with the container's network, so posting a pytest run, and reading its events, needs the bearer token. Until 2026-09-30 anyone could do both.

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

## A weekly check on a self hosted worker, with a cloud fallback

Lighthouse and the broken link crawl need a real browser, so they run in `checks/`, a Node 22 worker on a machine of my own rather than in the API image. For each page listed under `portfolio.pages` in `mercury.yaml`, the hourly scheduler Job posts a `lighthouse` and a `broken_links` check whenever none of that kind was created for that page in the last 7 days, and leaves them pending. The worker claims them through `POST /checks/claim` with the bearer token, which cannot claim any other run type, runs them, and posts a summary of about 1 KB.

A pending `lighthouse` check that no self hosted worker claims within `CHECK_CLAIM_WINDOW`, 30 minutes by default, is taken by the Python worker and run on the PageSpeed Insights API. A `broken_links` check never falls back, because PageSpeed cannot crawl, so it waits for the machine to wake. A posted result closes the check whatever it says.

The crawl follows same origin links breadth first to depth 3 and 200 pages, with a 10 second timeout per request, skips what `robots.txt` disallows, and reports only 4xx, 5xx, timeouts and refused connections, keeping the first 50 with the page each was found on. On the machine that runs it, `docker compose -f checks/compose.yml up -d --build` starts the worker with Chromium and `restart: unless-stopped`, reading `API_BASE_URL` and `MERCURY_BEARER_TOKEN` from a gitignored `.env` beside it.

The worker polls once an hour, set by `POLL_SECONDS`. It first polled once a minute, and because Container Apps keeps a replica while requests arrive less than 5 minutes apart, the API held one replica from 2026-09-28 14:25 UTC until the worker was stopped at 04:29 UTC the next day. It reached zero replicas 6 minutes after the last poll.

`tests/test_checks_integration.py` runs in CI's compose job against the stack's own runs page, with the window cut to 20 seconds and PageSpeed pointed at a port where nothing listens, so the fallback records an error as its finding without calling Google. Real Lighthouse runs in the checks container. From CI run 459c206:

```
self hosted lighthouse check f5e9bc68-93a6-4be0-a9cc-d145a4703702: {"lcp_ms": 2275, "scores": {"seo": 0.82, "performance": 0.97, "accessibility": 1, "best-practices": 0.78, "agentic-browsing": 0.5}, "tbt_ms": 0, "final_url": "http://proxy:8000/", ...}
self hosted broken_links check d0c0a33a-4aac-41c4-8e71-7edac46dbb50: {"broken": [], "broken_count": 0, "pages_checked": 1, "robots_skipped": 0, "page_limit_reached": false}
cloud fallback took check f6e6ab0a-4925-4001-ada3-094b9de1b7d8 after 21.0s: {"error": "<urlopen error [Errno 111] Connection refused>"}
tests/test_checks_integration.py::test_a_lighthouse_check_runs_on_the_self_hosted_worker PASSED
tests/test_checks_integration.py::test_a_broken_links_check_runs_on_the_self_hosted_worker PASSED
tests/test_checks_integration.py::test_a_lighthouse_check_falls_back_to_the_cloud_when_the_worker_is_offline PASSED
tests/test_checks_integration.py::test_a_broken_links_check_never_falls_back PASSED
================= 6 passed, 234 deselected in 74.12s (0:01:14) =================
```

The six include the two replica tests. The weekly schedule:

```
tests/test_scheduler.py::test_schedule_weekly_checks_creates_a_lighthouse_and_a_crawl_per_page PASSED
tests/test_scheduler.py::test_schedule_weekly_checks_creates_nothing_inside_the_week PASSED
tests/test_scheduler.py::test_schedule_weekly_checks_creates_the_next_once_the_week_is_up PASSED
tests/test_scheduler.py::test_schedule_weekly_checks_is_due_per_kind PASSED
tests/test_scheduler.py::test_schedule_weekly_checks_logs_and_carries_on_when_the_api_refuses PASSED
```

## Telegram, and its cold start

The bot is how I reach the service from a phone. Telegram posts each message to `POST /telegram`, which checks the secret token header first and refuses everyone when no secret is configured, then checks the chat id against an allowlist of one. Any other chat gets a 200 and no reply, and its text is not logged. `/status`, `/runs` and `/cancel` are answered in the webhook with no model call. `/cancel` writes the run's `done` event and clears its claim, so a worker still running it fails its next fenced write and stops.

Free text gets an "On it." reply at once and becomes a `chat` run for the worker, which holds the model key, so the webhook never waits on a model. The model sees the task types it may start and the last 20 turns of the chat, and answers with either a run to create or one question. The run it creates takes over the "On it." message and edits it after every step, so a run appears in the chat as one message that changes rather than one message per step. A model that still fails after its retries closes the run, and the message says so. Every update that passes the checks gets a 200 even when the reply fails to send, because Telegram retries anything else and a retried `/cancel` would run twice. The tests stand `tests/telegram_fake.py` in for Telegram's API.

```
tests/test_telegram_webhook.py::test_a_request_without_the_secret_is_refused[None] PASSED [  4%]
tests/test_telegram_webhook.py::test_a_request_without_the_secret_is_refused[wrong] PASSED [  8%]
tests/test_telegram_webhook.py::test_an_unconfigured_secret_refuses_everyone PASSED [ 12%]
tests/test_telegram_webhook.py::test_another_chat_gets_no_reply_at_all PASSED [ 16%]
tests/test_telegram_webhook.py::test_an_update_that_is_not_a_text_message_gets_no_reply PASSED [ 20%]
tests/test_telegram_webhook.py::test_runs_lists_the_latest_runs_newest_first PASSED [ 25%]
tests/test_telegram_webhook.py::test_runs_with_none_says_so PASSED       [ 29%]
tests/test_telegram_webhook.py::test_status_reports_each_site_and_the_day PASSED [ 33%]
tests/test_telegram_webhook.py::test_cancel_closes_an_unfinished_run_and_says_so PASSED [ 37%]
tests/test_telegram_webhook.py::test_cancel_stops_a_running_worker_at_its_next_write PASSED [ 41%]
tests/test_telegram_webhook.py::test_cancel_explains_what_it_could_not_do[/cancel-Usage: /cancel <run id or its first 8 characters>] PASSED [ 45%]
tests/test_telegram_webhook.py::test_cancel_explains_what_it_could_not_do[/cancel abc-Usage: /cancel <run id or its first 8 characters>] PASSED [ 50%]
tests/test_telegram_webhook.py::test_cancel_explains_what_it_could_not_do[/cancel 00000000-No unfinished run starts with 00000000.] PASSED [ 54%]
tests/test_telegram_webhook.py::test_cancel_leaves_a_finished_run_alone PASSED [ 58%]
tests/test_telegram_webhook.py::test_a_failed_reply_still_answers_telegram_with_200 PASSED [ 62%]
tests/test_telegram_webhook.py::test_each_answer_logs_how_long_after_the_message_was_sent PASSED [ 66%]
tests/test_progress.py::test_render_shows_each_step_and_never_the_code_or_the_test_output PASSED [ 70%]
tests/test_progress.py::test_render_summarises_a_lighthouse_and_a_crawl_result PASSED [ 75%]
tests/test_progress.py::test_a_worker_run_edits_its_one_message_after_every_step PASSED [ 79%]
tests/test_progress.py::test_a_run_nobody_asked_for_on_telegram_sends_nothing PASSED [ 83%]
tests/test_progress.py::test_a_refused_run_says_why PASSED               [ 87%]
tests/test_progress.py::test_a_telegram_failure_does_not_stop_the_run PASSED [ 91%]
tests/test_progress.py::test_push_progress_with_no_client_does_nothing PASSED [ 95%]
tests/test_progress.py::test_a_self_hosted_check_result_edits_the_message PASSED [100%]
============================= 24 passed in 26.44s ==============================
```

### The cold start, measured

Each answer logs how long after the message was sent it went out, from the `date` Telegram stamps on every message. On the first message after the API has scaled to zero, that figure is the cold start as the sender feels it. On 2026-09-30 the API reached zero replicas at 23:06:06 UTC, and a `/status` sent at 23:32:24 produced this line, exactly as Log Analytics holds it for container `agent-runs-api`:

```
{"timestamp": "2026-09-30T23:32:42.999490+00:00", "level": "INFO", "logger": "agent_runs.telegram", "message": "answered /status 19.0 s after it was sent", "trace_id": "b18accea459617d4a05e09b5aff56475", "span_id": "b45154bf7451c07c"}
```

Telegram's `date` has whole second resolution, so the true figure lies between 19.0 and 20.0 seconds. The Container Apps system log for the same wake shows where it went. The replica was assigned 2.4 seconds after the message was sent, the 166 MB image was pulled in 7.7 seconds and done by 10.0, the container started at 13.2, one startup probe failed at 14.2, and the reply went at 19.0.

The first measurement, on 2026-09-29 with a 132 MB image, was 26.1 seconds, of which about 14 went on the image pull. The image grew by 34 MB when git was added for repo chores, yet the pull took half as long, so pull time varies between wakes by more than the image size explains. These are two samples, not a distribution. A warm `/status` on 2026-09-28 answered in 2.6 seconds, and free text got its "On it." in 2.1 seconds.

## A pull request from a Telegram message

A message asking for a change to a repo listed in `mercury.yaml` becomes a `repo_chore` that waits for an Approve button. Once approved, the worker clones the repo, branches as `agent/<run id>`, lets the model rewrite whole files, and runs the repo's own `test_command`. The branch is pushed and a pull request opened only when the tests pass. Three red attempts end the run failed with nothing pushed, and the chat offers an Open it anyway button.

`tests/test_repo_chore_github.py` runs that whole path against the real public repo `thomas-whitley/mercury-fixture`, with the stub model and the fake Telegram. The message goes into the webhook, the chat run turns it into a chore, the test presses Approve, and the worker opens a real pull request. The test then checks the pull request on GitHub, and closes it and deletes its branch whatever happened. It needs a token that can push to the fixture, so it is marked `integration` and skips without one.

```
$ MERCURY_GITHUB_TOKEN=... uv run pytest tests/test_repo_chore_github.py -m integration -o addopts= -v -s
tests/test_repo_chore_github.py::test_a_telegram_message_opens_a_pull_request_on_the_fixture
{"timestamp": "2026-09-30T10:49:08.263610+00:00", "level": "INFO", "logger": "agent_runs.repo_chore", "message": "repo chore a645b409-1756-4ba1-ab22-28c8978b5bf3 ended succeeded", ...}
run a645b409-1756-4ba1-ab22-28c8978b5bf3 opened https://github.com/thomas-whitley/mercury-fixture/pull/2
PASSED
============================== 1 passed in 10.76s ==============================
```

From the Approve press to the chore ending took 5.3 seconds on the first run, covering the clone, the fixture's tests, the push and the two GitHub calls. [Pull request 2](https://github.com/thomas-whitley/mercury-fixture/pull/2) stays on the fixture, closed, as the record.

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

Two more caps sit above a run's own budget, and both are configuration too. `DAILY_TOKENS_PER_PROVIDER` is 500,000 tokens a day for each provider and `MONTHLY_BUDGET_USD` is 5. Before a run that calls a model starts, the worker adds up the tokens every step has recorded today on its provider, and this month across all of them. Gemini's free tier counts as nothing toward the month, and Haiku is charged at its output price on every token, since a run stores total tokens only, so the monthly figure can only overstate the bill. A tripped cap ends the run with one `done` event whose status is `budget` and names the cap, and sends one Telegram message to the owner's chat. A run that spends its own `TOKEN_BUDGET` sends the same one message.

```
tests/test_budget.py::test_a_provider_over_its_daily_tokens_trips PASSED
tests/test_budget.py::test_under_the_daily_cap_nothing_trips PASSED
tests/test_budget.py::test_yesterday_and_other_providers_do_not_count_today PASSED
tests/test_budget.py::test_the_month_trips_on_dollars_across_providers PASSED
tests/test_budget.py::test_free_tier_tokens_cost_nothing_toward_the_month PASSED
tests/test_budget.py::test_a_tripped_run_ends_with_one_event_and_one_message PASSED
tests/test_budget.py::test_a_chat_run_is_held_to_the_caps_too PASSED
tests/test_budget.py::test_a_trip_with_no_owner_chat_still_ends_the_run PASSED
tests/test_budget.py::test_a_run_that_spends_its_own_budget_sends_one_message PASSED
tests/test_budget.py::test_the_caps_are_config[DAILY_TOKENS_PER_PROVIDER-1234] PASSED
tests/test_budget.py::test_the_caps_are_config[MONTHLY_BUDGET_USD-2.5] PASSED
============================== 11 passed in 9.02s ==============================
```

The loop talks to a `Model` protocol with one `complete` method. As of Mercury step 1 the provider is no longer read from `MODEL`: each task type in `app/tasks.py` names a provider, and `PROVIDERS` in `app/config.py` maps that name to a kind, a base URL, a model, and the env var holding its key. `pytest` names `gemini`, whose key is `MODEL_API_KEY`; the registry also carries `haiku`, whose key is `ANTHROPIC_API_KEY`, for the task types Mercury adds later. A run whose provider has no key configured is refused, the same way a run past the daily limit is, so its stream closes with `status: refused` rather than staying claimed forever. `.env.example` lists both key variables.

## Cost

Target $0 a month idle: Container Apps free grant at scale to zero, Supabase free plan, GitHub free tier. The only variable cost is model tokens during a demo, capped per run and per day.

## Licence

MIT.
