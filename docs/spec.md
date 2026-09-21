# Spec: resumable streaming agent service (working name `agent-runs`)

Written 2026-09-21, session job-hunt-96, at Thomas's request after the Chipforge.ai pack. Status: proposed, not started. Owner: Thomas, on his own time, own GitHub account (`thomas-whitley`), nothing from Surefire in it.

## Why

Four gaps recur across the saved ads and none has a shipped artefact behind it:

| Gap | Ads that name it (of 63 saved, counted 2026-09-21) | Chipforge essential? |
|---|---|---|
| A named cloud with CI/CD deploy | 32 | nice to have (they say AWS) |
| Retrieval / RAG | 24 | nice to have |
| SSE or WebSocket streaming with reconnect and resume | Chipforge, and every "agentic loops are streaming" ad | yes |
| Stateful service on more than one instance | Chipforge | yes |

The playbook already commits to a FastAPI in Docker on Azure Container Apps with a small retrieval layer, deployed by GitHub Actions (Strategy, 2026-09-21). This spec adds the two Chipforge essentials to that same service so one repo closes all four. Same shape as `claude-run-cost`: public, MIT, README that shows measurements rather than adjectives.

Rule 1 of the application text rules still applies until this ships: no letter or CV names any of these gaps. When it ships, the CV gets one bullet and the letters get one sentence, both written from the README's measured facts.

## What it must make true

Each line below is a sentence the CV or a letter will be allowed to say once the test in the last column passes.

| Claim | Proof in the repo |
|---|---|
| An agent loop streams its steps over SSE and a client that drops mid run reconnects and resumes from the last event it saw, with no step replayed or lost | `tests/test_resume.py`: kill the connection after step N, reconnect with `Last-Event-ID: N`, assert steps N+1 onward arrive exactly once and the run completes |
| Run state lives outside the process, so two replicas serve the same run and a reconnect to the other replica resumes correctly | `tests/test_two_replicas.py` against two local containers behind one port (docker compose), and the same test in CI against the deployed app at two replicas |
| Step handling is idempotent: a retried step that already committed is a no-op | `tests/test_idempotent.py`: deliver the same step result twice, assert one row and one event |
| Retrieval over a small corpus feeds the loop | `tests/test_retrieval.py`: a query returns the expected chunk ids from pgvector |
| Deployed by GitHub Actions to Azure Container Apps with two replicas, tracing on every request | the workflow file, a deploy badge, and one screenshot or log excerpt in the README of a trace spanning both replicas |

## Architecture

One FastAPI service, one Postgres, N stateless replicas.

```
client ──SSE──> replica A ─┐
   (reconnect)             ├──> Postgres (runs, steps, events, chunks)
client ──SSE──> replica B ─┘         ▲
                                      │
                    worker loop (same image, `--role worker`)
                    claims a run, executes steps, appends events
```

- **API replicas** only read. They serve `GET /runs/{id}/events` as SSE by tailing the `events` table for that run. They never hold run state in memory beyond the current response.
- **Worker** (one or more, same image) claims a run with `UPDATE runs SET claimed_by = $worker WHERE id = $id AND claimed_by IS NULL` (idempotent claim), executes the loop, writes one `steps` row and one `events` row per step in a single transaction.
- **Postgres** is the only shared state. A new Supabase project on the free plan, separate from NextSet's (decided 2026-09-21: as cheap as possible, and no shared pool with a real app). Enable the `vector` extension in its dashboard. The CV still says "PostgreSQL" and "Container Apps"; it does not need to say Azure Postgres. The Supabase project pauses after a week without traffic on the free plan, so the deploy workflow runs a one row `SELECT` daily on a schedule to keep it awake, which also makes a free uptime check.
- **The agent loop** is deliberately small: plan, retrieve, act, verify, repeat until the verifier passes or the budget runs out. The verifiable task should be one where the answer checks itself, in the Chipforge spirit: for example, write a Python function that passes a supplied test file, run pytest in a subprocess, feed failures back. Claude API via the Anthropic SDK, with a token budget per run and per step recorded on each step row.

## Data model

```sql
runs      (id uuid pk, task text, status text, claimed_by text null,
           token_budget int, tokens_used int, created_at, finished_at null)
steps     (run_id fk, seq int, kind text, input jsonb, output jsonb,
           tokens int, started_at, finished_at, primary key (run_id, seq))
events    (run_id fk, id bigint generated always as identity, seq int,
           payload jsonb, created_at)          -- what SSE sends; id is the SSE event id
chunks    (id serial, source text, body text, embedding vector(1536))
```

Idempotency: `steps` primary key `(run_id, seq)`; the worker inserts with `ON CONFLICT DO NOTHING` and reads back, so a retried step cannot double write. `events.id` is monotonic per table, which is what `Last-Event-ID` carries.

## The resume protocol (the part the ad is asking about)

1. Client opens `GET /runs/{id}/events`. Server sends every existing event for the run with `id: <events.id>`, then keeps the connection open and polls (or `LISTEN/NOTIFY`) for new rows.
2. Every 15 seconds with nothing to send, server writes a comment line `: keepalive` so proxies and Container Apps ingress do not close the idle stream.
3. Client drops. On reconnect the browser or client sends `Last-Event-ID`. Server replays only rows with `id > Last-Event-ID`. Nothing about the client is stored server side; the events table is the cursor.
4. Terminal event `event: done` with the final status. A client that reconnects after `done` receives the remaining events and `done` again, which is harmless because the client treats `done` as idempotent.
5. Failure modes to test and document in the README: partial write (kill the server mid event; the client must see either the whole event or nothing, which SSE framing gives for free if each event is one `write` call), dropped client (server detects the disconnect and stops the tail, worker unaffected), reconnect to the other replica (nothing to do, by design; the test proves it).

## Deployment

- `Dockerfile`, one image, entrypoint switches on `ROLE=api|worker`.
- `docker-compose.yml` for local: postgres with pgvector, two api replicas, one worker, nginx or Caddy round robin on one port. This is what the two replica test uses locally.
- `infra/`: Bicep for a resource group, a Container Apps environment on the consumption plan, two apps and a Log Analytics workspace. No database resource; the Supabase connection string is a Container Apps secret.
  - `api`: min replicas 0, max 2, scale rule on concurrent HTTP requests with a low threshold (say 2) so a demo with two open streams brings up the second replica. Scaling to zero is what makes it free; cold start on the first request is a few seconds and is fine for a demo.
  - `worker`: min replicas 0, max 1, KEDA `postgresql` scaler on `SELECT count(*) FROM runs WHERE status = 'pending' OR claimed_by IS NULL AND status = 'running'`. The worker only exists while there is work. That is a second sentence for the letters ("scales to zero on a database query") at no cost.
- `.github/workflows/deploy.yml`: on push to main, run every test against compose (this is where the two replica proof lives, free), build and push the image to GHCR (free for public repos), `az containerapp update` via an OIDC federated credential, then one smoke request against the live URL. No stored cloud secret; the Anthropic key and the Supabase connection string are GitHub secrets passed into Container Apps secrets. A second workflow on a daily schedule pings Supabase and the live health endpoint.
- Live two replica proof: once, by hand, set `api` min replicas to 2, run `test_two_replicas.py` against the live URL, take the trace screenshot, set min back to 0. The README shows the screenshot and says exactly that. Continuous proof is the compose test in CI.
- Observability: OpenTelemetry FastAPI instrumentation exporting to Azure Monitor through the Log Analytics workspace (free up to 5 GB a month; this will use megabytes), trace id carried on every event payload so a client can quote it.
- Model: Claude Haiku 4.5 for the loop by default, with the model name in config so a demo can switch to Sonnet. Token budget per run capped at 50k in config, corpus embedded once at deploy and cached in the `chunks` table, so a run costs cents. A `MAX_RUNS_PER_DAY` guard in the worker (a count on `runs.created_at`) stops a public demo URL burning the API key.
- Cost, decided 2026-09-21 (Thomas: as cheap as possible): target $0 a month idle. Container Apps consumption plan has an always free monthly grant (180,000 vCPU seconds, 360,000 GiB seconds, 2 million requests) which two scale to zero apps will not exhaust in demos. Supabase free. GitHub Actions and GHCR free on a public repo. Log Analytics free tier. The only variable cost is Anthropic API tokens during demos and CI runs, cents each with Haiku and the budget cap; the CI test loop should use a stub model so pushes cost nothing. Do not add a custom domain, a paid Postgres, a Redis or a static IP; none is needed for any claim in the table above. If Azure ever bills more than a few dollars, the Bicep can be torn down with one command and the repo and README remain the evidence.

## Out of scope (say so in the README)

WebSocket (SSE only; one transport done properly beats two done badly). Multi tenant auth. Self hosted inference (Ollama is a stretch goal, a second worker role that swaps the Anthropic client). Retrieval quality work (the corpus is a few dozen chunks; the point is the plumbing). Any UI beyond a `curl` example and a 40 line HTML page that uses `EventSource` and shows reconnect working.

## Build order

Each task ends with its test green. Nothing is claimed until the row in "What it must make true" passes.

1. Repo, Dockerfile, compose with Postgres and one api replica, health endpoint, `pytest` in CI. (Evening.)
2. Schema and the `events` tail as SSE with `Last-Event-ID`. `test_resume.py` against one replica. (Evening.)
3. Worker role, idempotent claim and step insert, the small agent loop with a token budget. `test_idempotent.py`. (Weekend day.)
4. Second replica in compose behind one port; `test_two_replicas.py`. Fix whatever breaks (it will be keepalives or the tail cursor). (Evening.)
5. pgvector, embed the corpus at startup, retrieval step in the loop. `test_retrieval.py`. (Evening.)
6. Bicep, OIDC, deploy workflow, live smoke test, OpenTelemetry to Azure Monitor. (Weekend day; Azure setup always takes longer than planned.)
7. README with the measurements: resume test output, the two replica trace screenshot, tokens per run, cost per month. Then the CV bullet and the letter sentence, through the usual pending and approval path.

About four evenings and two weekend days. Fits the six hours a week outside applications over three to four weeks, or one focused long weekend.

## What the CV will be allowed to say afterwards (draft, not approved)

One bullet under Projects, replacing nothing: "agent-runs | 2026, github.com/thomas-whitley/agent-runs. FastAPI agent loop that streams its steps over SSE and resumes from the last event after a dropped connection, run state in PostgreSQL so two Container Apps replicas serve one run, deployed by GitHub Actions with Bicep, traced end to end." Letter sentence for streaming and state essentials: written from whichever test output is in the README, numbers included. Both go through `drafts/pending/` and the check like everything else.

## Decisions from the grilling, 2026-09-21 afternoon (all Thomas's answers; do not re-ask)

- Cloud. Azure Container Apps. Personal free Azure account created tonight with a $5 budget alert on day one. Bicep plus a five line `az deployment group create` script. GitHub to Azure auth by OIDC federated credential, no stored secret.
- Database. a new free Supabase project named for this repo. NextSet's project is not touched.
- Embeddings. Voyage AI (free tier), pgvector. Fallback if the key or tier is a problem: Postgres full text search, and the README then says "retrieval", not "vector".
- Corpus. the repo's own docs plus a few hundred Python standard library doc snippets, so retrieval changes the agent's output on the pytest task.
- The verifiable task. write a Python function that passes a supplied pytest file. Verification is pytest's exit code in a `subprocess` with a timeout and no network inside the worker container. The README calls that an honest demo sandbox.
- Model and guards. Claude Haiku 4.5 by default, 50k tokens per run, 20 runs a day on the public URL, stub model in CI. All four in config.
- Cost. $0 a month idle. No paid resource is added without Thomas's yes.
- Repo. `thomas-whitley/agent-runs`, public from the first commit, MIT. `uv`, `ruff`, `pytest`. TDD per task. The README claims table is updated only when the matching test is green. A 40 line HTML demo page with `EventSource` and a "kill connection" button.
- Where and who. built on Thomas's other laptop under his personal Claude account, which has the same `thomas-whitley` GitHub access. This session seeds the repo (spec, build brief, `CLAUDE.md`, README stub, licence) and Thomas pushes it with `tools/publish-agent-runs.sh`; the other laptop clones and builds. Communication between the two machines is through git only.
- When. tonight, 2026-09-21. Tasks 1 to 5 first while the installs run (Docker Desktop, Azure CLI, `uv`, account signups). Deploy cut off 23:00; past it the README marks the deployed proof pending and the deploy happens another evening.
- Applications. everything on hold until the README is done, including the finished Chipforge pack. Thomas weighed the risk that the Chipforge ad closes meanwhile. Wednesday's sourcing pass still runs, script only, saving ad files for FULL leads and building no packs.

## Open questions for Thomas (all answered above; kept for the reasoning)

1. Azure (the earlier decision, and the word most ads use) or AWS (Chipforge's word)? The service is identical; only `infra/` and the workflow change. Recommendation: Azure, because it was decided already, 32 ads name a cloud with Azure the most common, and Container Apps has an always free grant where AWS App Runner and Fargate do not (Lambda would be free but does not hold an SSE stream open past its timeout without extra work).
2. DECIDED 2026-09-21: Supabase Postgres on the free plan. Thomas asked for the project to be as cheap as possible; the cost line above is the consequence.
3. The verifiable task for the loop. Recommendation: generate a function that passes a supplied pytest file. It is small, it verifies itself, and it is the same idea as Chipforge's "the design either simulates or it doesn't".

Constraint from Thomas, 2026-09-21: as cheap as possible. Any future change to this spec that adds a paid resource needs his yes first.
