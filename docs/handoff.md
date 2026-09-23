# Handoff: Mercury steps 2a and 2b done, ready for 2c (2026-09-23, ~21:30 Melbourne)

## Where things stand

The original agent-runs build is finished, Mercury steps 0 and 1 are done, and step
2a closed on the live deploy on 2026-09-23. The scheduler Job
`agent-runs-scheduler` ran from an `az containerapp job start`, logged
`scheduled run a9f4b60f-4ff1-4f6f-872e-cbb61a216c62 created with no client`, and
that `site_check` run closed `succeeded` with 0 tokens in 84 ms. README claim
"A scheduled task runs with no client connected" is green with that proof pasted
under it.

Step 2b is done in code and not yet pushed. `POST /checks/claim`,
`POST /checks/{id}/heartbeat` and `POST /checks/{id}/result` are in
`app/check_claims.py`, behind the bearer token, with 22 tests in
`tests/test_claim_endpoints.py`. 184 tests pass locally, plus the integration tests
that need the compose stack.

The live API is
`https://agent-runs-api.grayriver-8b441372.australiaeast.azurecontainerapps.io`.

The next build step is **2c, the `checks/` Node worker, Lighthouse only**, specified
in `docs/build-brief-mercury.md`. It talks to the three endpoints above. One decision
should be made before step 3 and is written up below under "Two deploys write to
the same resources".

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
- **The lease is 60 seconds, not two minutes.** The brief says the endpoints "reuse
  the existing two minute lease", but `LEASE_SECONDS` defaults to 60 in
  `app/config.py` and nothing sets it in the Bicep or compose. The endpoints reuse
  that setting, so they get 60. `docs/mercury.md` also says the worker heartbeats
  every 30 seconds. Decide which number is meant before 2c picks a heartbeat
  interval.
- **The scheduler's uptime runs still have no `done` event.** Their streams close
  on `finished_at`, but a client never sees how they ended. The result endpoint
  writes one. The scheduler could do the same in a small follow up.

## The private config repo

`thomas-whitley/mercury-config` is private and was created on 2026-09-23 from
`config/private-repo/`. It holds the real `mercury.yaml`, which lists the live
API's `/health` as the one site and `thomas-whitley/agent-runs` as the one repo.
`telegram.chat_id` is still 0 and gets set in step 3. Its workflow sets the config
on the Job with `az containerapp job secret set` and pins the api, the worker and
the Job to one public image tag, currently `ea2f791`. Its Actions secrets are only
the three Azure ids. It signs in as the same app registration as this repo, with
its own three federated credentials added by `REPO=thomas-whitley/mercury-config
./scripts/setup-oidc.sh`.

`config/private-repo/deploy.yml` in this repo is still the full template for the
later steps, and it is out of date. It names `ghcr.io/thomas-whitley/mercury:v0.2.0`,
an image that does not exist yet, and its floating tag check refused every pinned
tag (the private repo has the fixed version). Bring it into line when step 3
touches the private workflow.

## Two deploys write to the same resources

This is unresolved and should be decided before step 3. This repo's Deploy
workflow runs the full Bicep on every push to `main`. The private repo's
workflow then changes some of the same resources by hand. Each public deploy
therefore undoes three things.

1. It puts the Job's placeholder config back. After any push here, run
   `gh workflow run deploy.yml -R thomas-whitley/mercury-config` or the Job checks no
   sites until you do.
2. It moves the api and worker to the newest public image, which breaks the rule in
   `docs/mercury.md` that "a public commit cannot change what talks to the phone
   before its owner has read it".
3. Once step 3 has the private repo set Telegram secrets on the api, a public
   deploy will remove them, because the Bicep sends each app's full secret list.

One way out is for this repo to stop deploying once the private repo owns the
deployment, and only build and publish the image. That needs the user's decision.

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

### Test database separation matters

The unit tests use `TEST_DATABASE_URL`, default `agent_runs_test`, which the fixtures
create. The integration tests use `AGENT_RUNS_DATABASE_URL`, default `agent_runs`,
which is the database the running stack serves from. Pointing the unit tests at
`agent_runs` means a running compose worker claims runs the tests just created. The
fixtures refuse a non local host unless `ALLOW_REMOTE_TEST_DB` is set, because they
drop the public schema and `.env` holds a real Supabase URL.

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

Deploy runs when the repo variable `DEPLOY_ENABLED` is `true`, which it is. Repo
variables are `DEPLOY_ENABLED`, `MODEL` and `MODEL_BASE_URL`. Secrets are
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `DATABASE_URL`,
`MODEL_API_KEY` and `MERCURY_BEARER_TOKEN`. `LIVE_URL` is not set, so the keepalive
workflow skips its health request and only wakes the database. No secret value has
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
```
