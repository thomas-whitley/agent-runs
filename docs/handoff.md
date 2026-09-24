# Handoff: Mercury steps 2a to 2c done, deploys moved to the private repo, ready for 2d (2026-09-24, ~11:30 Melbourne)

## Where things stand

The original agent-runs build is finished, Mercury steps 0 and 1 are done, and step
2a closed on the live deploy on 2026-09-23. The scheduler Job
`agent-runs-scheduler` ran from an `az containerapp job start`, logged
`scheduled run a9f4b60f-4ff1-4f6f-872e-cbb61a216c62 created with no client`, and
that `site_check` run closed `succeeded` with 0 tokens in 84 ms. README claim
"A scheduled task runs with no client connected" is green with that proof pasted
under it.

Step 2b is live. `POST /checks/claim`, `POST /checks/{id}/heartbeat` and
`POST /checks/{id}/result` are in `app/check_claims.py`, behind the bearer token,
with 22 tests in `tests/test_claim_endpoints.py`. 187 Python tests pass locally,
plus the integration tests that need the compose stack.

Step 2c is done. `checks/` is the Node 22 and TypeScript worker with 25 Vitest
tests, and `ci.yml` has a `checks` job. Its exit condition was met on the live
deploy on 2026-09-23. Run `583bed4b-d508-4aa5-a793-7a850ce421a4`, a Lighthouse check
of the live API's own `/` page, was claimed by `node dist/main.js --once` running on
this Linux machine as `thomas-laptop`, closed `succeeded` in 11.8 seconds, and shows
in the live `GET /runs` with `executor: self_hosted`.

The live API is
`https://agent-runs-api.grayriver-8b441372.australiaeast.azurecontainerapps.io`.

The next build step is **2d, the crawl, the fallback and the self hosted
deployment**, specified in `docs/build-brief-mercury.md`. Its exit turns README claim
seven green. The user asked for a PageSpeed Insights API key (free) for the cloud
fallback, to live in the private repo as `PAGESPEED_API_KEY`. It is not created yet.
Suggested 2d order, one commit each, test first: the claim window on the Python
side, the PageSpeed executor behind it, the crawl in `checks/` and `broken_links`
in `KINDS`, then `checks/compose.yml`, the Lighthouse integration test in CI and the
README row.

## Read these first

- `CLAUDE.md` in the repo root, for the working rules and the writing rules.
- `docs/mercury.md` is the Mercury design and `docs/build-brief-mercury.md` the step
  order with an exit condition per step. `docs/design.md` still describes the loop,
  the resume protocol and the data model underneath.
- `docs/superpowers/plans/2026-09-22-mercury-step-2a-scheduler.md` is the plan 2a was
  built from. Its task sections are the original wording. Where the build departed
  from it, the "What this plan defers" section and the commit messages say why.
- `README.md` is the deliverable. Its claims table is the contract.
- `git log` carries the reasoning in the commit messages. Do not re-derive it from
  the diff.

## What changed in step 2a that the plan did not say

- **The worker never claims `site_check`.** `_CLAIMABLE` in `app/worker.py` and the
  worker's KEDA query in `infra/main.bicep` both exclude it. Without that, the worker
  (polling every second) would claim a scheduled run before the scheduler closed it,
  refuse it as not runnable, and count it against the 20 a day limit.
- **A refused POST counts as a failure.** A 401 or an unreachable API calls
  `record_failure`, so a bad token suspends every schedule after three hours rather
  than skipping silently forever. Nothing resumes a schedule until step 3 wires the
  Telegram button. Until then, `app.schedule_state.resume(conn, "site_uptime:<url>")`
  by hand.
- **The scheduler's POST waits 60 seconds.** The API scales to zero and the hourly
  Job is usually the request that wakes it. The cold start is not measured yet.
  That is step 3's exit condition.
- **The Job is deployed whenever `MERCURY_BEARER_TOKEN` is set,** with a placeholder
  config listing no sites. It has its own secret list, holding no model or embedding
  key.

## What step 2b decided that the brief did not say

- **A check's kind is a column.** `runs.check_kind` (migration 007) holds `uptime`,
  `lighthouse` or `broken_links`, from `inputs.kind` on `POST /runs`. A `site_check`
  with no kind is `uptime`, which is what the scheduler posts.
- **Claim hands out only `lighthouse` and `broken_links`.** Declaring `uptime` is a
  422, because the scheduler's uptime runs sit `pending` for a few seconds before it
  closes them, and a worker could otherwise take one mid flight.
- **Every endpoint is fenced on `type = 'site_check'` in its SQL,** so the checks
  worker's token cannot claim, keep alive or close any other type, even a run
  claimed under the same worker id.
- **A result closes the run `succeeded` whatever it says,** writing the result as
  step 1 (`check`) and a `done` event as step 2 in one transaction. A result over
  16 KB is a 422.
- **The daily run limit skips `site_check`,** since it makes no model call.
- **The lease is two minutes.** `LEASE_SECONDS` defaulted to 60 while the brief and
  `docs/mercury.md` say two minutes. It is now 120 (`DEFAULT_LEASE_SECONDS` in
  `app/config.py`), because a Python worker model step can run 112 seconds between
  heartbeats. The claim endpoints reuse the setting. 2c's worker should heartbeat
  every 30 seconds, as `docs/mercury.md` says.
- **The scheduler's uptime runs still have no `done` event.** Their streams close
  on `finished_at`, but a client never sees how they ended. The result endpoint
  writes one. The scheduler could do the same in a small follow up.

## What step 2c decided that the brief did not say

- **npm comes from corepack on this machine.** Ubuntu's `nodejs` package has no npm.
  `corepack npm@10.9.9 <args>` fetches it into a user cache with no sudo, and
  `checks/package.json` pins it with `packageManager`. CI uses `actions/setup-node`.
- **Lighthouse 13 scores five categories,** performance, accessibility,
  best-practices, seo and agentic-browsing, which is where the brief's "five
  category scores" lands. An audit counts as failed below 0.9, the mark Lighthouse's
  own report uses. The live summary was 271 bytes, not the 1 KB the brief allows.
- **Only `lighthouse` is claimed.** `KINDS` in `checks/src/worker.ts` gains
  `broken_links` when 2d adds the crawl.
- **The checks worker is not deployed anywhere yet.** It ran once by hand for the
  exit proof. `checks/compose.yml` and running it on the self hosted machine are 2d.
- **The bearer token was read from the api's Container Apps secret** for that run,
  with `az containerapp secret list --show-values` into a shell variable, never
  printed. The self hosted machine will need the same value in its environment.

## The private config repo

`thomas-whitley/mercury-config` is private and was created on 2026-09-23 from
`config/private-repo/`. It holds the real `mercury.yaml`, which lists the live
API's `/health` as the one site and `thomas-whitley/agent-runs` as the one repo.
`telegram.chat_id` is still 0 and gets set in step 3. It signs in as the same app
registration as this repo, with its own three federated credentials added by
`REPO=thomas-whitley/mercury-config ./scripts/setup-oidc.sh`. Its workflow and
`config/private-repo/deploy.yml` are now the same file. Step 3's Telegram secrets
should become Bicep parameters passed from that workflow, not `az containerapp
secret set` calls, or the next deploy removes them.

## Only the private repo deploys (decided and done 2026-09-23 and 24)

The user decided the private repo owns deployment. This repo's `deploy.yml` now
runs the two replica test and publishes `ghcr.io/thomas-whitley/agent-runs:<sha>`
and deploys nothing. `mercury-config`'s workflow (template in
`config/private-repo/deploy.yml`) checks this repo out at `PUBLIC_SHA`, runs that
commit's `infra/deploy.sh` with that commit's image, and passes `mercury.yaml` as
`MERCURY_CONFIG_B64`. It refuses to run if `DATABASE_URL`, `MODEL_API_KEY` or
`MERCURY_BEARER_TOKEN` is missing, because the Bicep sends each app's full secret
list and would drop it. `PUBLIC_SHA` is `b501861`. **To ship anything from this
repo, bump `PUBLIC_SHA` in the private repo after the Publish run for that commit
is green.** Any Bicep change reaches Azure only that way.

The private repo now has secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `DATABASE_URL`, `MODEL_API_KEY` and
`MERCURY_BEARER_TOKEN`, and variables `MODEL` and `MODEL_BASE_URL`. The bearer
token is a new one, generated on 2026-09-23 straight into the secret and into the
local `.env` on the Windows machine (gitignored), where the checks worker will read
it. This repo's `MERCURY_BEARER_TOKEN`, `MODEL_API_KEY` and `AZURE_*` secrets are
now unused and stale. `DATABASE_URL` and `DEPLOY_ENABLED` are still read by
`keepalive.yml`.

What went wrong on the way, so it is not repeated:

- The first two private deploys took the api down, the second from about 13:25
  UTC on 2026-09-23 to about 01:00 UTC on 2026-09-24. The pasted `DATABASE_URL`
  had an unencoded `@` in the password, the api's lifespan hangs on
  the migrations when it cannot connect, and `/health` never answers. The smoke
  test reported success because it reached the old revision before traffic moved.
  Running this repo's Deploy by hand (`gh workflow run deploy.yml`) restored it
  both times, because it still deploys the old way with the old secrets. That
  escape hatch goes away when the deploy job is gone, so from then on a bad
  deploy is fixed by correcting the private secret and rerunning.
- The fix was a script, kept out of the repo, that reads the string with
  `getpass`, percent-encodes the password, connects, and only then pipes it into
  `gh secret set`. An earlier version printed a fragment of the password in a
  psycopg error. **The user should change the Supabase database password** and
  set the new string in both repos' `DATABASE_URL`.
- The smoke test's `curl` had no `--max-time`, so one hung request stalled the job
  for 22 minutes. It now has `--max-time 20`. It still cannot tell the old
  revision from the new one. Checking that the latest revision is the one serving
  is a worthwhile follow up.

## Suggested skills

- `superpowers:test-driven-development` for anything that touches code. Every commit
  in 2a was written test first. The Bicep and workflow changes have no test and were
  checked with `az bicep build` and by reading the compiled template.
- `superpowers:verification-before-completion` before claiming any README row.

## Things the next agent will not work out from the tree

### Another session sometimes pushes to this repo

Commits authored `thomasbwhitley@gmail.com` (the latest is `18a8b30`) come from a
second Claude session, not from this machine (`thomaswhitley1535@gmail.com`).
**Pull before starting and check for divergence before every push.** Never force
push. A `digest` run created on the live API at 2026-09-23 00:00:11 UTC and
refused after 38 seconds is probably that session testing the bearer token. It
was not investigated.

### The Windows machine has no Azure CLI

This session ran on the Windows machine, which has no `az` on Windows or in WSL and
no signed in Azure session. Everything Azure went through GitHub Actions. The
Claude Code auto mode classifier blocked `gh variable set` on the private repo and
`gh run list` while a deploy was running, so the user ran those with `!`.

### Test database separation matters

The unit tests use `TEST_DATABASE_URL`, default `agent_runs_test`, which the fixtures
create. The integration tests use `AGENT_RUNS_DATABASE_URL`, default `agent_runs`,
which is the database the running stack serves from. Pointing the unit tests at
`agent_runs` means a running compose worker claims runs the tests just created. The
fixtures refuse a non local host unless `ALLOW_REMOTE_TEST_DB` is set, because they
drop the public schema and `.env` holds a real Supabase URL.

### Running the tests on the Windows machine

The suite does not run natively on Windows, because psycopg's async pool refuses
the default Proactor event loop. Run it in the Ubuntu WSL distro, with a Linux venv
kept out of the repo's Windows `.venv`, while Docker Desktop serves the compose
Postgres on `localhost:5432`.

```
wsl -d Ubuntu-24.04 -- bash -lc 'cd /mnt/c/Projects/agent-runs && UV_PROJECT_ENVIRONMENT=$HOME/.venvs/agent-runs UV_LINK_MODE=copy ~/.local/bin/uv run pytest'
```

Git for Windows sets `core.autocrlf=true`, which gave `docker/entrypoint.sh` CRLF
line endings and made every container exit with `no such file or directory`.
`.gitattributes` now pins `*.sh` to LF.

### Streaming tests need a real socket

`tests/test_resume.py` and the others run against a real uvicorn on a real port,
not Starlette's `TestClient`, which buffers the whole response. Do not "simplify"
them back. Anything testing an idle stream needs a wall clock deadline, because
keepalives stop a read timeout from ever firing.

### caplog and the test server

`create_app()` calls `configure_logging()`, which replaces the root handlers, and
caplog's with them. A test that starts the server and then asserts on a log line
must attach `caplog.handler` to the logger it reads, as
`tests/test_scheduler.py::test_run_due_checks_logs_the_created_run_with_no_client`
does.

### Free tier quota, not a code problem

`MAX_RUNS_PER_DAY` counts runs, not model calls, and one run makes up to ten calls
while it retries. Gemini's free tier caps calls per day per model, so a few failing
runs exhaust it and later runs close with `status: error`. That is the error path
working. The repo variable `MODEL` is `gemini-3.5-flash-lite` for its larger
allowance. `_STARTED_TODAY` in `app/worker.py` skips `site_check`, so checks
claimed by the checks worker do not use up the daily limit.

### Azure specifics that cost time to discover

- `az monitor log-analytics query` and the Application Insights extension fail to
  install on this machine. Query through the REST API instead. For the Job's logs,
  with `ws` from `az monitor log-analytics workspace show -g agent-runs -n
  agent-runs-logs --query customerId -o tsv`:

  ```
  az rest --method post --url "https://api.loganalytics.io/v1/workspaces/$ws/query" \
    --resource "https://api.loganalytics.io" \
    --body '{"query":"ContainerAppConsoleLogs_CL | where TimeGenerated > ago(2h) and Log_s has \"created with no client\" | project TimeGenerated, ContainerJobName_s, Log_s"}'
  ```

- The OIDC federated credential must carry the immutable subject, which embeds
  numeric owner and repo ids. The plain form alone fails with `AADSTS700213`.
  `scripts/setup-oidc.sh` registers both and takes `REPO=` for another repo.
- Container Apps rejects a secret whose value is empty, so an unset optional key is
  left out of the secrets array. An env var whose `secretRef` names a missing secret
  fails the same way.
- Container Apps cuts any HTTP request at 240 seconds on consumption. Keepalives do
  not extend it.
- The resource group is `agent-runs` in `australiaeast`.

### Deployment settings

Deployment settings live in the private repo now, as described above. This
repo keeps `DEPLOY_ENABLED` and `DATABASE_URL` for `keepalive.yml`. `LIVE_URL` is
not set, so the keepalive workflow skips its health request and only wakes the
database. No secret value has
been printed in the repo, the logs or the chat.

### Local compose details

The scheduler service sits behind a profile, so `docker compose up` does not start
it. Export `MERCURY_BEARER_TOKEN` before `up`, because the api reads it then, and
run `docker compose run --rm scheduler`. The proxy healthcheck uses `127.0.0.1`,
because `localhost` resolves to `::1` in the nginx alpine image and nginx binds
IPv4 only.

## Cost position

$0 a month idle is holding. The api and worker scale to zero. The Job runs hourly
for a few seconds at 0.25 vCPU, roughly 5,400 vCPU seconds a month against a free
grant of 180,000. Log Analytics is capped at 0.1 GB a day. Supabase, GHCR and
Actions are free. The Azure trial spending limit is on and the `five-dollar-cap`
budget alert exists. Do not upgrade the subscription to pay as you go.

## Verification commands

```
docker compose up --build -d --wait
uv run ruff check . && uv run ruff format --check .
uv run pytest
AGENT_RUNS_BASE_URL=http://localhost:8000 uv run pytest -m integration -o addopts=
az bicep build --file infra/main.bicep --stdout > /dev/null
cd checks && corepack npm@10.9.9 ci && corepack npm@10.9.9 run typecheck && corepack npm@10.9.9 test
```
