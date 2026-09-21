# Handoff: agent-runs, ready for task 7 (2026-09-21, ~23:45)

## Where things stand

`thomas-whitley/agent-runs` is built and deployed. Tasks 1 to 6 of the original build
order are done, all five rows of the README claims table are green, and the service
is live on Azure Container Apps at
`https://agent-runs-api.grayriver-8b441372.australiaeast.azurecontainerapps.io`.
A run posted to that URL streams plan, retrieve, act and verify over SSE and finishes
`succeeded` in one attempt on 223 tokens. 90 tests pass locally, plus 2 integration
tests that need the compose stack. CI and Deploy are both green on `main` at `27fad6c`.

The next session is task 7: the README's final measurements and the 40 line demo page
(`static/index.html`, `EventSource` plus a kill connection button, served by the api
and screenshotted into the README). Nothing blocks it.

## Read these first

- `docs/design.md` is the only design document. It replaced `docs/spec.md` and
  `docs/build-brief.md`, which were deleted in `32b0044` by a different session.
- `CLAUDE.md` in the repo root, for the working rules and the writing rules.
- `README.md` is the deliverable. Its claims table is the contract.
- `git log` from `b96a594` onwards is this session's work, and the commit messages
  carry the reasoning. Do not re-derive it from the diff.

## What is in progress or half done

Nothing is half written. Two loose ends, both decisions rather than code:

1. **`CLAUDE.md` line 18 contradicts the README.** It still mandates the sentence
   "a subprocess with a timeout and no network inside the worker container" and says
   "The README says exactly that". That sentence is false: the worker container has a
   network, because it calls the model and Postgres. The README and `docs/design.md`
   were corrected to describe what the sandbox actually does. Thomas was told and has
   not yet decided whether to change `CLAUDE.md`. Do not "fix" the README back to
   match it without asking.

2. **Tracing is configured but traces nothing.** Verified against the live
   deployment on 2026-09-21. The exporter works and reaches Azure Monitor, but the
   only rows in Application Insights are the SDK fetching its own configuration.
   Three separate faults: `FastAPIInstrumentor.instrument_app` is called inside the
   lifespan in `app/main.py`, which is after Starlette builds its middleware stack,
   so there are no `requests` rows; `app/worker.py` never calls
   `configure_telemetry`, so the agent loop emits nothing; and no service name is
   set, so `cloud_RoleName` is `unknown_service`. The draft CV bullet ends "traced
   end to end" and **that phrase cannot ship until this is fixed**. The README
   claims row deliberately says only "Deployed to Azure Container Apps by GitHub
   Actions with OIDC".

   The fix is roughly: move `instrument_app` into `create_app()` after the routes
   are added, call `configure_telemetry()` from the worker's `main()`, and set
   `OTEL_SERVICE_NAME` per role in the Bicep. Then redeploy, post a run, and
   confirm a single trace carries two `cloud_RoleName` values. Query without the
   broken az extension:

   ```
   az rest --method post \
     --url "https://api.applicationinsights.io/v1/apps/154a7510-caa6-4649-b858-ba28d5b21abd/query" \
     --resource "https://api.applicationinsights.io" \
     --body '{"query":"union requests, dependencies | where timestamp > ago(1h) | summarize count() by itemType, cloud_RoleName"}'
   ```

3. **Task 5's Voyage path is unproven.** `VOYAGE_API_KEY` is empty, so retrieval runs
   on Postgres full text search. The pgvector path is implemented and tested against a
   real database with an offline embedder, so only the embedding provider is unproven.
   The README says "retrieval", not "vector", deliberately. If a Voyage key appears,
   note that `chunks.embedding` is `vector(1536)` and Voyage models are not all 1536
   dimensions, so the column may need altering.

## Suggested skills

- `superpowers:test-driven-development` for anything that touches code. Every commit in
  this session was written test first, including the two bugs the code review found.
- `superpowers:verification-before-completion` before claiming any README row.
- `frontend-design` for the demo page, which is the one piece of UI in the repo.

## Things the next agent will not work out from the tree

### Another session is pushing to this repo

Commits authored `thomasbwhitley@gmail.com` come from a second Claude session on
another machine, not from this one (`thomaswhitley1535@gmail.com`). It deleted the
planning docs (`32b0044`) and added a code review brief (`ad36d0f`) mid flight, which
was worked through and deleted in `27fad6c`. **Pull before starting and check for
divergence before every push.** Two agents on one branch will collide. Never force
push.

### Test database separation is load bearing

The unit tests use `TEST_DATABASE_URL`, default `agent_runs_test`, which the fixtures
create. The integration tests use `AGENT_RUNS_DATABASE_URL`, default `agent_runs`,
which is the database the running stack serves from. These are deliberately different
variables. Pointing the unit tests at `agent_runs` means a running compose worker
claims runs the tests just created and interleaves its own steps into them, which
surfaces as a verify step that is somehow an act. That cost an hour to find.

The fixtures refuse a non local host unless `ALLOW_REMOTE_TEST_DB` is set, because they
drop the public schema and `.env` holds a real Supabase URL.

### Streaming tests need a real socket

`tests/test_resume.py` and the others run against a real uvicorn on a real port, not
Starlette's `TestClient`. The in process client buffers the whole response, so a test
written against it passes while proving nothing about a mid stream disconnect. Do not
"simplify" them back to `TestClient`.

Anything testing an idle stream needs a wall clock deadline, not a read timeout.
Keepalives keep bytes flowing, so a read timeout never fires and the test hangs
forever instead of failing.

### Free tier quota, not a code problem

`MAX_RUNS_PER_DAY` counts runs, not model calls, and one run makes up to ten calls
while it retries. Gemini's free tier allows 20 calls a day per model. A few failing
runs exhaust it and every later run closes with `status: error`, which is the error
path working, not a bug. `gemini-3.5-flash-lite` is configured in the repo variables
because it has the larger allowance. `gemini-3.8-flash` was exhausted on 2026-09-21.

### Azure specifics that cost time to discover

- The OIDC federated credential must carry the **immutable** subject, which embeds
  numeric owner and repo ids:
  `repo:thomas-whitley@320883245/agent-runs@1379154329:ref:refs/heads/main`.
  The plain `repo:owner/repo:...` form alone fails with `AADSTS700213`.
  `scripts/setup-oidc.sh` registers both and derives the ids from the GitHub API.
- Container Apps rejects a secret whose value is empty. An unset optional key must be
  left out of the secrets array entirely, not passed as `''`. The Bicep builds
  `optionalSecrets` and `modelEnvironment` conditionally for this reason.
- Container Apps cuts any HTTP request at **240 seconds** on consumption. Keepalives do
  not extend it and raising it needs paid premium ingress, which is out of scope. A run
  longer than that has its stream cut and the client must reconnect. This is documented
  in the README and is worth showing on the demo page.
- The GHA build cache needs `docker/setup-buildx-action`; the default docker driver
  cannot export a cache.
- Providers `Microsoft.App`, `Microsoft.OperationalInsights` and `Microsoft.Insights`
  are registered. The resource group is `agent-runs` in `australiaeast`.

### Deployment is gated on purpose

The `deploy` job only runs when the repo variable `DEPLOY_ENABLED` is `true`, so a push
to a repo not wired to Azure does not fail. Repo variables currently set:
`DEPLOY_ENABLED`, `MODEL`, `MODEL_BASE_URL`. Secrets set: `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `DATABASE_URL`, `MODEL_API_KEY`.
No secret value has been printed anywhere in the repo, the logs or the chat.

### Local proxy detail

The compose proxy healthcheck uses `127.0.0.1`, not `localhost`. In the nginx alpine
image `localhost` resolves to `::1` first and nginx binds IPv4 only, so the container
reports unhealthy forever with `localhost`.

## Cost position

$0 a month idle is holding. Both Container Apps scale to zero, Log Analytics is capped
at 0.1 GB a day in the Bicep, Supabase is free, GHCR and Actions are free on a public
repo. The Azure trial spending limit is on and the `five-dollar-cap` budget alert
exists. The only variable cost is model tokens, and the model in use is a free tier.
Do not upgrade the Azure subscription to pay as you go.

## Verification commands

```
docker compose up --build -d --wait
uv run ruff check . && uv run ruff format --check .
uv run pytest
AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest -m integration -o addopts=
```

The integration tests are excluded from the default run by `addopts` in
`pyproject.toml`. Both CI jobs must stay green on every push.
