# Build brief: agent-runs, first session

Paste this as the first message to the build session on the other laptop, after cloning `thomas-whitley/agent-runs`. Everything below was decided on 2026-09-21; do not re-ask it. The full design is `docs/spec.md` in this repo; read it in full first, then `CLAUDE.md`.

## What we are building tonight

A FastAPI service where a client posts a pytest file, an agent writes the function that passes it, and the client watches the steps arrive over SSE. If the client drops, it reconnects with `Last-Event-ID` and resumes from the next step, even on a different replica. Postgres (Supabase) is the only state. Retrieval over a small corpus (repo docs plus Python standard library snippets) feeds the loop through pgvector and Voyage embeddings. Deployed to Azure Container Apps at scale to zero by GitHub Actions with OIDC. Cost target $0 a month idle.

## Setup Thomas does himself, first thing, while the code work starts

These need a browser, a card or an admin prompt, so the agent does not do them.

1. Docker Desktop installed and running (reboot if it asks). `docker compose version` prints a version.
2. `uv` installed. `uv --version` works.
3. Azure CLI installed. `az login` done against a new personal free Azure account. Set a $5 budget alert in Cost Management before anything is deployed.
4. A new free Supabase project for this repo (not NextSet's). Enable the `vector` extension in the dashboard. Copy the connection string into a local `.env` (gitignored) as `DATABASE_URL`.
5. A Voyage AI API key into `.env` as `VOYAGE_API_KEY`. An Anthropic API key as `ANTHROPIC_API_KEY`.
6. `gh auth status` shows `thomas-whitley`.

## Task order (from the spec; each ends with its test green, then a commit)

1. Repo skeleton: `pyproject.toml` with `uv`, `ruff`, `pytest`; `Dockerfile` (one image, `ROLE=api|worker` entrypoint); `docker-compose.yml` with Postgres plus pgvector and one api replica; `GET /health`; one passing test; `.github/workflows/ci.yml` running `uv run pytest` and `ruff`. Push; CI green.
2. Schema (`runs`, `steps`, `events`, `chunks` as in the spec) as plain SQL migrations applied at startup. `POST /runs` creates a run. `GET /runs/{id}/events` streams the `events` table as SSE with `id:` per event, replays from `Last-Event-ID`, sends `: keepalive` every 15 seconds. `tests/test_resume.py` against one replica: disconnect after step N, reconnect with `Last-Event-ID: N`, assert N+1 onward arrive exactly once.
3. Worker role: idempotent claim (`UPDATE ... WHERE claimed_by IS NULL`), one transaction per step writing `steps` and `events`, `ON CONFLICT DO NOTHING` on `(run_id, seq)`. The agent loop: plan, retrieve (stub for now), act (Haiku 4.5 through the Anthropic SDK, stub model in tests), verify (pytest in a subprocess, 10 second timeout, no network), repeat until pass or the 50k budget is spent. `tests/test_idempotent.py`.
4. Second api replica in compose behind one port (Caddy or nginx round robin). `tests/test_two_replicas.py`: start a stream on one replica, drop it, reconnect and land on the other, assert resume. Expect keepalives or the tail cursor to break first; fix, do not work around.
5. Retrieval: embed the corpus at startup into `chunks` (Voyage; fall back to `tsvector` if the key fails and say so in the README), retrieval step returns the top chunks into the plan prompt. `tests/test_retrieval.py` with a fixed corpus.
6. Azure: `infra/main.bicep` (resource group scoped: Container Apps environment on consumption, `api` min 0 max 2 scaling on HTTP concurrency 2, `worker` min 0 max 1 with a KEDA `postgresql` scaler on pending runs, Log Analytics; no database resource, Supabase URL as a secret). `infra/deploy.sh`. OIDC federated credential set up by a short `az ad` script Thomas runs. `.github/workflows/deploy.yml` on push to main: tests on compose, build and push to GHCR, `az containerapp update`, one smoke request. Daily scheduled workflow pings Supabase and `/health`. OpenTelemetry FastAPI instrumentation exporting to Azure Monitor. **Cut off 23:00.** Past it, the README's deploy row says pending and this task moves to another evening.
7. README: the claims table with test output pasted under each green row, the two replica trace screenshot (set `api` min replicas to 2 by hand once, run the test against the live URL, screenshot, set back to 0), tokens per run, cost per month, the honest sandbox sentence, what is out of scope. The 40 line demo page (`static/index.html` with `EventSource` and a kill connection button) is served by the api and screenshotted here.

## Guards that must exist before the URL is public

`MAX_RUNS_PER_DAY` (20) checked in the worker from `runs.created_at`. Token budget per run (50k) enforced in the loop. Both in config with the model name.

## What not to do

Do not add a paid resource. Do not add WebSocket, auth, a UI beyond the demo page, or Ollama tonight (stretch goals, listed in the spec). Do not mention Surefire, its customers or its code anywhere. Do not write a README claim before its test is green. Do not put a key in a commit or a log line.

## When done

Push. Thomas's job hunt session reads the README from GitHub and drafts the CV bullet and the letter sentence from the measured facts in it.
