# Design

`agent-runs` is a small service that runs an agent loop against a verifiable task and streams the steps to clients over Server-Sent Events. It exists to make one property true and provable: a client that drops mid run reconnects and receives the remaining steps exactly once, whichever replica it lands on. Everything else is in service of that.

## The task

A client posts a pytest file. The agent writes the Python function that makes it pass. Verification is pytest's exit code, run in a subprocess with a timeout, a scrubbed environment, and both of Python's socket layers disabled. That is a demo guard rather than isolation. The worker container has a network of its own, because it calls the model and Postgres, and the README says exactly that.

The task was chosen because the answer checks itself. The loop either produces a function that passes the tests or it does not, so "keep working until the result holds" has a concrete meaning.

## Architecture

One FastAPI service, one Postgres, N stateless replicas.

```
client ──SSE──> replica A ─┐
   (reconnect)             ├──> Postgres (runs, steps, events, chunks)
client ──SSE──> replica B ─┘         ▲
                                      │
                    worker (same image, ROLE=worker)
                    claims a run, executes steps, appends events
```

- API replicas only read. `GET /runs/{id}/events` tails the `events` table for that run as SSE. Nothing about a connected client is held in memory beyond the current response.
- The worker is the same image with `ROLE=worker`. It claims a run with a lease (`UPDATE runs ... WHERE claimed_by IS NULL`), executes the loop, and writes one `steps` row and one `events` row per step in a single transaction.
- Postgres is the only shared state. The stream is a live read of the events table, which is why any replica can serve any client.
- The agent loop is plan, retrieve, act, verify, repeat until the verifier passes or the token budget is spent.

## Data model

```sql
runs      (id uuid pk, task text, status text, claimed_by text null,
           token_budget int, tokens_used int, created_at, finished_at null)
steps     (run_id fk, seq int, kind text, input jsonb, output jsonb,
           tokens int, started_at, finished_at, primary key (run_id, seq))
events    (run_id fk, id bigint generated always as identity, seq int,
           payload jsonb, created_at)
chunks    (id serial, source text, body text, embedding vector)
```

`steps` has a primary key on `(run_id, seq)` and the worker inserts with `ON CONFLICT DO NOTHING`, so a retried step cannot double write. `events.id` is monotonic, and it is what `Last-Event-ID` carries. The exact schema is in `migrations/`.

## The resume protocol

1. A client opens `GET /runs/{id}/events`. The server sends every existing event for the run with `id: <events.id>`, then holds the connection open and tails for new rows.
2. With nothing to send, the server writes a `: keepalive` comment every 15 seconds so proxies and Container Apps ingress do not close an idle stream.
3. The client drops. On reconnect it sends `Last-Event-ID`. The server replays only rows with a greater id. The events table is the cursor; the server stores nothing about the client.
4. A terminal `event: done` carries the final status. A client that reconnects after `done` receives the tail and `done` again, which is harmless because `done` is idempotent on the client side.

Failure modes covered by tests and described in the README: a partial write (each event is one write, so the client sees a whole event or nothing), a dropped client (the server notices and stops the tail, the worker is unaffected), and a reconnect that lands on the other replica (nothing to do, by design, and `tests/test_two_replicas.py` proves it).

## Retrieval

A small corpus of Python reference notes is loaded into `chunks` at startup. When an embedding key is configured the retrieve step uses pgvector distance; without one it uses Postgres full text search with the query rewritten to match any term and rank. Both paths are tested. The repo's own docs were in the corpus at first and were removed because they share vocabulary with a pytest file while saying nothing about writing Python, so they crowded the useful chunks out.

## Model provider

The loop talks to a `Model` protocol with one `complete` method. `MODEL=stub` runs offline and is what CI uses, so a push costs nothing. An OpenAI compatible base URL and key cover any provider speaking that format. An Anthropic key uses the Anthropic SDK. The worker refuses to start with none of them set and says which to set.

## Guards

`TOKEN_BUDGET` per run and `MAX_RUNS_PER_DAY` on the public URL are configuration. A refused run still gets its `done` event so a waiting client is not left hanging.

## Deployment

- One `Dockerfile`; the entrypoint switches on `ROLE`.
- `docker-compose.yml` runs Postgres with pgvector, two api replicas behind nginx on one port, and one worker. This is where the two replica test runs, locally and in CI.
- `infra/main.bicep` declares a Container Apps environment on the consumption plan, an `api` app at min 0 max 2 replicas scaling on HTTP concurrency, a `worker` app at min 0 max 1 scaling on a KEDA postgresql query over pending runs, and a Log Analytics workspace. There is no database resource; the connection string is a secret.
- `.github/workflows/ci.yml` runs lint and tests on every push against the compose Postgres with the stub model. `deploy.yml` builds and pushes the image and updates the apps through an OIDC federated credential, with no stored cloud secret. `keepalive.yml` pings the database and the health endpoint daily so a free tier project does not pause.
- A run is one trace across both roles. `POST /runs` opens a root span and stores its
  W3C `traceparent` on the run row; the worker restores that context as current before
  the loop starts, so its five step spans (plan, retrieve, act, verify, done), each
  carrying the step kind, the provider, and its tokens, land as children of the api's
  root span rather than starting a second, unrelated trace. A run whose lease went
  stale and was picked up by a replacement worker gets one more child span, marked as
  a takeover, so the trace shows where the first worker's spans stop and the second's
  begin. `NULL` in `trace_context` means tracing was off when the run was created, or
  the run predates the column; a step then starts its own trace rather than raising.
  `FastAPIInstrumentor.instrument_app` runs
  from `create_app()`, not the lifespan: it works by patching `build_middleware_stack`,
  and Starlette calls that method itself the first time the app receives any ASGI
  scope, including the lifespan startup scope, which is before the lifespan
  function's own body runs. `OTEL_SERVICE_NAME` is set per role in the Bicep, so
  `cloud_RoleName` tells the api and the worker apart in Application Insights.

  Every process also writes structured JSON to stdout, which the managed environment
  ships to the workspace on its own. This is deliberately not routed through the
  telemetry exporter, so logs keep working in CI, in compose, and on a machine with
  no exporter configured. A line carries a timestamp, level, logger, message, and
  whatever the call site passed as `extra` (run id, worker id); the trace and span
  ids come from whatever span is current when the line is emitted. A task's input
  body and a model's output never appear in a log line.
- Container Apps cuts an HTTP request at 240 seconds on the consumption plan. Keepalives do not extend it and raising it needs paid premium ingress, so a run longer than that has its stream cut and the client reconnects with `Last-Event-ID`.

## Cost

Target $0 a month idle. Container Apps consumption plan has an always free monthly grant that two scale to zero apps do not exhaust in demos. Postgres on a free plan. GitHub Actions and the container registry are free for a public repo. Log Analytics free tier. The only variable cost is model tokens during a demo, capped per run and per day. No custom domain, no paid database, no Redis, no static IP.

## Out of scope

WebSocket transport (SSE done properly beats two transports done badly). Authentication and multi tenancy. Self hosted inference. Retrieval quality work beyond the plumbing. Any UI beyond a demo page and `curl`.
