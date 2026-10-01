# Handoff: step 5 done in code, not deployed yet (2026-10-01)

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

## Step 5 is done in code (2026-10-01), not deployed

Commits `4880b73` (5a cleanup), `e151332` (5b CI watch), `5de9ce5` (5c scheduler
secrets), `8e2bdc2` (5d dependency audit) and `a8b4ec6` (5e digest). 392 tests pass
locally, and CI and Publish are green on all five. Live is still `f9e7804`, held
back because the user had a job interview demo on the live page that afternoon.

- **Three decisions the user made.** The dependency audit reads lock files through
  the GitHub API and asks OSV.dev, rather than running `pip-audit` and `npm audit`,
  because the image has no Node. The digest arrives at 08:00 Melbourne, the first
  hourly tick after 07:30, rather than moving the Job's cron. The model writes the
  digest from gathered facts, and the plain facts go out if it never answers.
- **No new task types.** `ci_watch` and `dependency_audit` are `site_check` kinds
  in `SCHEDULER_CHECK_KINDS` (`app/tasks.py`), because the registry is frozen until
  every claim is green. The scheduler creates and closes them like uptime. Claim
  refuses them, and the orphan close covers all three scheduler kinds.
- **Nothing reads `schedule:` in `mercury.yaml`.** Every task is due by counting
  from its last run, as the weekly checks already were. The sample now says so, and
  `mercury-config`'s copy needs the same edit at the next `PUBLIC_SHA` bump.
- **The scheduler Job now holds the bot token and the GitHub token** (`5de9ce5`).
  Before that it held neither, so the live Job could suspend a site but never send
  the Resume message that 2a and 4a promised. The Bicep params already flow from
  `mercury-config`'s `deploy.yml`, so deploying is a `PUBLIC_SHA` bump.
- **`GitHubClient` moved** to `app/github.py` with its own `GitHubError`.
  `tests/github_fake.py` serves repo, workflow run, tree and raw content reads, and
  `tests/osv_fake.py` serves OSV's batch query and advisory reads.
- **Cleanup** strips bodies from finished non check runs older than 30 days (task,
  step input and output, event output, keeping seq, kind and the done status) and
  deletes runs older than 365 days, unlinking an open anyway run's `source_run_id`
  first. `tests/test_cleanup.py` is the brief's named exit for step 5.
- **Not yet proven live.** The digest has only met the stub model, since this
  machine has no Gemini key. The first live digest at 08:00 after the deploy is the
  check. The audit and CI read were run against the real GitHub and OSV.
- **Found on the way.** The audit flags `oauthlib` 3.3.1 (2 advisories) and `pyjwt`
  2.14.0 (1), both moderate, in this repo's `uv.lock`. The README's claims table has
  no row for "Two task types run on two providers in one deploy", one of the eight
  in `docs/mercury.md`, and 6d needs it.

## Ollama as a second provider (2026-10-01), not deployed

Both live chat runs had ended in error on Gemini 503s ("high demand"), so the user
chose Ollama's cloud free tier. `ollama` is `gpt-oss:120b` at
`https://ollama.com/v1`, keyed by `OLLAMA_API_KEY`, which is in `.env` and in
`mercury-config`'s Actions secrets (set 2026-10-01 03:07 UTC). chat runs on it and
falls back to gemini, and the Gemini types fall back to it (`FallbackModel` in
`app/model.py`, wired in `app/worker.py` `_with_fallback`). The first failure
switches the run and updates `runs.provider`. `infra/main.bicep` gives the key to
the worker alone, and **`mercury-config`'s `deploy.yml` needs the
`OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}` line** that
`config/private-repo/deploy.yml` now has, at the same deploy as the `PUBLIC_SHA`
bump. Once deployed, chat runs on ollama and the rest on gemini make the README
claim "Two task types run on two providers in one deploy" provable from run rows.

## Step 4a to 4d are done (2026-09-30), 4e is next

Local commits `ba23efd` (4a), `9faaf18` (4b), `93eea9d` (4c), `27e152c` (git in the
image), `e1d4bc7` (the token in the deploy) and `81f6903` (4d). 338 tests pass and
6 integration tests skip.

- **4a, approvals.** `app/approvals.py` and migration 009. One row per question,
  callback data `approval:<id>:yes|no`, answered once under `FOR UPDATE`, 24 hour
  expiry run by the hourly scheduler Job (`expire_due`). Actions are `start_run`,
  `resume_schedule` and `open_anyway`. A site's third failure now sends one message
  with a Resume button, and `/resume <site>` works. Both were promised in 2a.
- **4b, chore from chat.** Repos come from `portfolio.repos` in `mercury.yaml` as
  `{name, test_command}`. A chore is created `awaiting_approval`, which the worker
  never claims, and its progress lands on the question's message. `POST /runs`
  refuses `repo_chore` with 422.
- **4c, the executor.** `app/repo_chore.py`. The user chose whole file edits with 3
  attempts, a test command per repo, and a scrubbed environment subprocess (PATH,
  throwaway HOME, locale, `PYTHONDONTWRITEBYTECODE=1`) with a 600 s timeout. The
  branch is pushed only after green, so a branch on the remote means a takeover and
  the model is skipped. The token reaches git as `http.extraheader` through
  `GIT_CONFIG_*` env vars, never a file or URL. A background heartbeat on a second
  connection keeps the lease through a slow test run.
- **git in the image** adds 35 MB compressed (132 to 167 MB). The user chose one
  image. Remeasured on 2026-09-30 after deploying `66c0000`: 19.0 s from zero
  replicas, with a 7.65 s pull of the 166 MB image. The README carries both figures.
  Logs are read with `mercury-config`'s Logs workflow (`gh workflow run logs.yml -R
  thomas-whitley/mercury-config -f query='<KQL>'`, then `gh run view --log`), which
  signs in over OIDC, so no machine needs an Azure session. The Azure CLI is
  installed on the Windows machine but not signed in, because the Outlook account
  found no subscription.
- **The deploy.** `mercuryGithubToken` is a new Bicep param, worker only.
  `config/private-repo/deploy.yml` passes `MERCURY_GITHUB_TOKEN`, and **the private
  repo's copy needs the same line** before the next `PUBLIC_SHA` bump.
- **4d, open it anyway.** A failed chore from Telegram offers Open it anyway. The
  new run carries `source_run_id` (migration 011), reapplies the stored diff with no
  model call, and says in the PR body that the tests failed. A diff over 20,000
  characters is stored truncated and will not reapply.
- **Only Python repos** can have a chore today. The image has uv but no Node.
- **The chore user** (`8fb7b16`). The worker is root in the image, so a repo's
  test command runs as the unprivileged `chore` user, which cannot read the
  worker's keys from `/proc/<pid>/environ`. Proven in the built image, where
  `cat /proc/1/environ` got Permission denied. As root with no such user the chore
  stops. The other session (`9936eb9`) found the same `/proc` hole in the pytest
  sandbox and fixed it in `7693fc8` with a separate `sandbox` user, so a posted
  test file cannot reach a chore's checkout. `app/sandbox.py` has its own copies of
  the two helpers rather than a shared module.
- **4e is done (2026-09-30).** `thomas-whitley/mercury-fixture` is public, stdlib
  only (`python -m unittest -v`, so a chore installs nothing into the no-dev image),
  with `main` protected by a PR rule at 0 approvals and `enforce_admins` off. The
  fine grained `MERCURY_GITHUB_TOKEN` is in `.env` and in `mercury-config`'s secrets,
  and can push to the fixture and `agent-runs` but not see `mercury-config`.
  `tests/test_repo_chore_github.py` drives webhook, chat, Approve and worker against
  the real fixture with the stub model, and closes its PR and branch in teardown.
  It skips without the token, so CI skips it. README row green with PR 2 as the
  record. From Git Bash, run WSL scripts with `MSYS_NO_PATHCONV=1`, or `/mnt/c`
  paths get rewritten and `$(...)` inside `bash -lc '...'` came back empty.
- **Deployed.** `mercury-config` `922cccd` lists the fixture in `mercury.yaml`,
  passes `MERCURY_GITHUB_TOKEN` to the deploy, and pins `PUBLIC_SHA` at `66c0000`.
  `d19cfb9` added the Logs workflow. Step 5 is next.
- **6c brought forward (2026-10-01), for a job interview demo that afternoon.**
  No task type is public since `9936eb9`, so the brief's "bodies for public types"
  would have shown nothing. The user chose: with no Authorization header,
  `/runs/{id}/events` streams `{seq, kind}` and the done status, bodies stripped;
  a wrong token is still 401 (`76671f0`). `?after=` resumes like `Last-Event-ID`.
  The page is `#/runs/<id>` with Kill connection and Reconnect (`5cc3c80`). Also
  `a17becd` sets the `azure` logger to WARNING, and `0c4c537` closes uptime runs
  left pending and unclaimed for 10 minutes (the 21:00 orphan was the ingress
  dropping the POST with `RemoteDisconnected` and delivering it later). All
  deployed as `0c4c537` (`mercury-config` `8d0e5f1`).
- **The browser resume claim is green (2026-10-01).** A warm worker finishes a run
  in about 4 s, so a kill often lands before the first event. `f9e7804` marks that
  case with its own row, and is deployed. On the live page, run `ff9e0edd` was
  killed after #235 and reconnected 62 s later, and #236 to #238 arrived once each,
  ending `succeeded` (`9983140`). `~/post-run.sh` posts a live pytest run and prints
  its page URL. The orphaned `pending` uptime run has cleared.
- **A second session shared this working copy on 2026-09-30.** It was making
  `pytest` runs require the bearer token and editing README, `docs/mercury.md`,
  the Publish workflow and several tests. Both sessions running pytest at once
  dropped each other's schema in `agent_runs_test`, so run with
  `TEST_DATABASE_URL=postgresql://agent:agent@localhost:5432/agent_runs_test_chores`
  when another session is active.

## Step 3 is done (2026-09-29)

The Telegram bot is live as @tw_mercury_bot, and README claim "The webhook cold
start is measured and stated" is green at 26.1 s from zero replicas, with the live
log line pasted under it. **The next step is 4, approvals and repo chores**, then
5, 6c and 6d in the brief's order. Step 4 needs the public fixture repo
`thomas-whitley/mercury-fixture` (brief, setup item 4), which does not exist yet as
far as this session knows.

- **Commits.** 3a `3cf5cfa`, 3b `52c9b99`, 3c `efa37a6`, 3d `aa55bd4`, 3e `903d86c`,
  the chat retry fix `1904bfc`, the poll interval `024c94c`, the README `75d0618`.
  The commit messages carry the reasoning.
- **The webhook** is `app/telegram_webhook.py`. Secret header first, failing closed
  with no secret configured, then an allowlist of one chat id from `mercury.yaml`.
  `/status`, `/runs` and `/cancel` are answered there with no model call. Free text
  gets "On it." and becomes a `chat` run for the worker (`app/chat.py`), which keeps
  the last 20 turns per chat in `telegram_turns`. `/cancel` clears `claimed_by`, so a
  running worker's next fenced write fails and it stops.
- **Progress** is one message per run, edited after every step (`tests/test_progress.py`).
- **Budget caps** (3d, claim four) end a run with one `done` event whose status is
  `budget`, not a new event kind. Haiku is costed at 5 USD per million on every
  token as an upper bound.
- **The cold start.** The api reached zero replicas at 04:35:39 UTC on 2026-09-29,
  and `/status` sent at 04:36:55 was answered 26.1 s later (Telegram's `date` is
  whole seconds, so 26.1 to 27.1). System log: replica assigned at +2 s, image
  pulled by +16 s, container started at +19 s, one failed startup probe, reply at
  +26 s. The image pull is about 14 s of it, so a smaller image is the lever if the
  figure ever matters. Warm figures from 2026-09-28 are 2.6 s for `/status` and
  2.1 s for "On it.".
- **Why the api stopped scaling to zero.** The self hosted checks worker polled
  `POST /checks/claim` every 60 s, and Container Apps keeps a replica while requests
  arrive under 5 minutes apart. One replica ran from 2026-09-28 14:25 UTC to
  2026-09-29 04:34 UTC. `POLL_SECONDS` now defaults to 3600 (`024c94c`). Telegram
  itself had 0 pending updates and no delivery errors.
- **The worker is stopped.** It was stopped at 04:29 UTC for the measurement. The
  image is rebuilt with the new default. Start it again with
  `docker compose -f checks/compose.yml up -d --build` unless the user has already.
- **Free text fails while Gemini's free tier returns 503.** That is on Google's
  side, and the chat run closes and says so. Whether to fall back to Haiku is the
  user's call and is not made. The recommendation was to wait it out.
  `ANTHROPIC_API_KEY` is not deployed and the Bicep has no param for it, and Haiku
  costs money, so it needs the user's yes.
- **Deploying Telegram.** The private workflow passes `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_WEBHOOK_SECRET` from that repo's secrets, and `telegram.chat_id` is in
  its `mercury.yaml`. The webhook was registered with
  `scripts/register-telegram-webhook.sh <api url>`, which reads both values from
  `.env`. `getUpdates` returns nothing while the webhook is set. The private repo's
  `PUBLIC_SHA` is `1904bfc`, and `024c94c` and `75d0618` change nothing the api runs,
  so no redeploy is needed for them.
- **Reading the timing line.** `Log_s has "answered"` on `ContainerAppConsoleLogs_CL`
  for `agent-runs-api`, with the query pattern under "Azure specifics" below. Startup
  events are in `ContainerAppSystemLogs_CL`, column `Reason_s`.
- **npm** is now installed on this Linux machine (Ubuntu's `npm` 9.2.0 beside
  `nodejs` 22.22.1), so `cd checks && npm ci && npm test` runs locally. 43 tests pass.

## Step 2d is done (2026-09-28)

README claim seven, "A weekly check runs on a self hosted worker and falls back to
the cloud path when it is offline", is green. **The next step is 3, the Telegram
webhook**, then 4, 5, 6c and 6d in the brief's order.

- **The crawl** is `checks/src/crawl.ts`: same origin, breadth first, depth 3, 200
  pages, 10 second timeout, robots.txt for `mercury-checks` or `*`. It reports 4xx,
  5xx, timeouts and refused connections (status `"error"`), the first 50 listed and
  the rest counted. `KINDS` is `lighthouse` and `broken_links`.
- **The weekly schedule** was not in the brief but claim seven says "weekly". The
  hourly Job now also posts a `lighthouse` and a `broken_links` check for each URL
  under `portfolio.pages`, when none of that kind was created for that page in 7
  days (`schedule_weekly_checks` in `app/scheduler.py`). It reads no cron line; the
  sample's `lighthouse` cron line is gone. The private `mercury.yaml` lists the live
  runs page as the one page.
- **The self hosted deployment** is `checks/Dockerfile` (Node 22, Debian Chromium,
  `CHROME_EXTRA_FLAGS="--no-sandbox --disable-dev-shm-usage"`) and
  `checks/compose.yml` with `restart: unless-stopped`. It runs on this Linux laptop
  as `thomas-laptop`, reading `checks/.env` (mode 600, gitignored), which holds
  `API_BASE_URL`, `WORKER_ID` and the live bearer token, read from the api's
  Container Apps secret straight into the file and never printed. **Docker here is
  socket activated (`docker.service` disabled, `docker.socket` enabled), so after a
  reboot the worker is not back until something touches Docker.** `sudo systemctl
  enable docker` fixes that.
- **The integration test** is `tests/test_checks_integration.py`, in CI's compose
  job only. That job makes a throwaway bearer token, sets `CHECK_CLAIM_WINDOW=20`
  and `PAGESPEED_URL` to a port where nothing listens (the override is new in
  `app/pagespeed.py`), and runs the checks worker with `docker compose --profile
  checks run --rm checks node dist/main.js --once`. It skips in Deploy's verify job,
  which has no token.
- **Live proof.** `PUBLIC_SHA` is `972ac0d1da7e739b9e8839e7a6ae1010714e695d`
  (`mercury-config` commit `3056ebb`, Deploy run 36422397083). A Job run started by
  hand at 12:34 UTC created broken_links check `0df02af5`, which `thomas-laptop`
  claimed and closed at 12:34:59 UTC. No lighthouse check was created, because run
  `583bed4b` from 23 September is within the week; the first scheduled one comes
  on the first hourly run a full week after run `583bed4b` was created on 23 September.
- `PAGESPEED_API_KEY` is still unset in `mercury-config`, so a live fallback calls
  PageSpeed keyless, whose shared quota was spent on 2026-09-24.

## Steps 6a and 6b are live (2026-09-28)

The runs page is live at
`https://agent-runs-api.grayriver-8b441372.australiaeast.azurecontainerapps.io/`,
revision `agent-runs-api--0000032`, from the image
`ghcr.io/thomas-whitley/agent-runs:54885ff99db3c850584ecef4d9caaaf8987bcb69`.
`PUBLIC_SHA` in `mercury-config` is `54885ff` (its commit `70c0fbb`, Deploy run
36417745019 green). That bump from `b501861` also took 2d's claim window and the
PageSpeed fallback live. `PAGESPEED_API_KEY` is not set in the private repo, and the
Bicep leaves the secret out when it is empty, so the fallback calls PageSpeed with
no key.

- **6a.** `web/` is Vite, React and TypeScript on Node 22, npm pinned with
  `packageManager` and run through corepack on this machine. The Dockerfile builds it
  in a Node stage and copies `web/dist` next to `app`. FastAPI serves it from a catch
  all GET registered after every API route (`tests/test_web_page.py`), and
  `index.html` goes out with `Cache-Control: no-cache` so a redeploy never leaves a
  browser asking for deleted asset hashes. `WEB_DIST_DIR` overrides where it looks.
  `ci.yml` and `deploy.yml` both have a `web` job, and Publish waits for it.
  `static/` and the task 7 demo page are gone, so there is no kill connection button
  until 6c.
- **6b.** The page reads `GET /runs` 50 at a time and shows short id, created time
  (in the browser's time zone), type, provider, executor, status, tokens and
  duration. Older runs follows `next_cursor` and appears only when there is one. A
  null provider or executor shows as `none`; the server writes `cloud` itself for a
  check the cloud ran. `web/test/runs-list.test.tsx` has 8 tests, covering the page
  boundary against a fake of the endpoint's contract.
- **What the live list shows.** On 28 September it was almost all the hourly
  uptime `site_check` runs, succeeded in 56 to 205 ms with 0 tokens. That is the
  real data and was left alone. Before recording, one public `pytest` run and one
  Lighthouse check claimed by the laptop worker give it some variety.
- The README section on the page now says what it shows. Claim six is unchanged
  until 6c.
- Deferred: a failed Older runs load shows the error with no retry until reload,
  and the Python page tests use a stand in `dist`.

## The order changed on 2026-09-28: 6a and 6b before the rest of 2d

Thomas needs the runs page on screen for a recording on Wednesday 30 September,
so steps 6a and 6b of the brief come next, ahead of the rest of 2d and ahead of
steps 3 to 5. 2d stops where it stands. The claim window and the PageSpeed
fallback are in (8bc9e4f to e88d0de). The crawl, `broken_links` in `KINDS`,
`checks/compose.yml`, the Lighthouse integration test in CI and README claim seven
all wait. **After 6b the next step was the rest of 2d, not 6c. 2d is now done, see above.** Then follow the
brief's order unchanged: 3, 4, 5, 6c, 6d. Decision 43 holds: nothing claims React
until 6b's exit is met on the live deploy, and claim six stays as it is until 6c.

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
