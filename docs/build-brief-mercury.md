# Build brief: Mercury, first session

Paste this as the first message to the build session on the other laptop, after `git pull` in the agent-runs working copy. Everything below was decided on 2026-09-22 across four rounds of questions; do not re-ask it. Read `docs/mercury.md` in full first, then `docs/design.md`, then `CLAUDE.md`.

## Before Mercury: task 7 of agent-runs

Task 7 has not landed and comes first. It is the demo page (`static/index.html`, about 40 lines, `EventSource` plus a kill connection button, served by the api at `/`) and the README's final measured numbers. Push it as its own commit, wait for CI and Deploy to go green, then start below. The demo page becomes the runs page in step 6, so keep it to one static file with no framework.

## Setup Thomas does himself, first thing, while step 1 starts

These need a browser, so the agent does not do them and does not ask for the values in chat. Each goes straight into the place named.

1. A Telegram bot from BotFather. Token into the private config repo's Actions secrets as `TELEGRAM_BOT_TOKEN` and into a local `.env`. Note the bot's username.
2. His own Telegram chat ID (message the bot once, read `getUpdates`). Goes into the private config as `telegram.chat_id`. It is not a secret but it is not public either.
3. A fine grained GitHub personal access token, one year expiry, scoped to `agent-runs`, `nextset`, `claude-run-cost`, the profile README repo, `ludo-electrical` and the new `mercury-fixture`, permissions contents and pull requests, read and write. Into Actions secrets as `MERCURY_GITHUB_TOKEN` and into `.env`.
4. A public throwaway repo `thomas-whitley/mercury-fixture` with one passing test and branch protection on `main`.
5. A private repo `thomas-whitley/mercury-config` seeded from `config/private-repo/` in this repo.
6. A random 32 byte hex string as `TELEGRAM_WEBHOOK_SECRET` and another as `MERCURY_BEARER_TOKEN`, both into Actions secrets and `.env`.
7. A Gemini API key (free tier) as `MODEL_API_KEY` with `MODEL_BASE_URL` set to Google's OpenAI compatible endpoint. The Anthropic key already in `.env` stays for the Haiku 4.5 task types.
8. Docker installed on the machine that will run `checks/`, with the bearer token in a local gitignored `.env` beside `checks/compose.yml`. On Windows or Mac, set Docker Desktop to start on login, or the worker does not come back after a reboot and every check quietly takes the cloud fallback.
9. A PageSpeed Insights API key if the free quota needs one, as `PAGESPEED_API_KEY`. The cloud fallback uses it.

## Step order (from `docs/mercury.md`; each ends with its test green, then a commit)

1. **Task registry, provider per type, and the run list endpoint.** `app/tasks.py` with a `TaskType` dataclass (name, tools, provider, budget, public) and the five entries. `runs.type` column by migration, default `pytest` for existing rows, plus a nullable `executor` column used only by checks. `POST /runs` takes `type` and `inputs`; the worker picks the provider from the registry, not from `MODEL`. Config gains `PROVIDERS` as a mapping of name to base URL, key variable and model. **`GET /runs` with a serializer**: metadata only (type, provider, executor, status, tokens, duration), newest first, keyset pagination on `(created_at, id)`, default 50 and max 200, behind the existing public rate limit. It is here rather than with the page because the chat tools and `curl` both want it first. `tests/test_tasks.py` and `tests/test_run_list.py`. Existing tests still green.
2. **Scheduled Job, site checks, and the self hosted check worker.** `infra/main.bicep` gains a Container Apps Job, cron trigger, that runs the same image with `ROLE=scheduler` and posts the due tasks to the api with the bearer token. `site_check` type: HTTP status and latency for the URL, no model call. Schedule read from the mounted config. Log line `scheduled run <id> created with no client` and the first README claim green from it.

   Then the second executor. Three endpoints behind the bearer token: `POST /checks/claim` (body declares the kinds the worker can run, returns one pending `site_check` of those kinds with its lease, or 204), `POST /checks/{id}/heartbeat`, `POST /checks/{id}/result`. **Claim must never return a `pytest`, `chat`, `repo_chore` or `digest` run; write the test that proves it.** `checks/` is a Node 22 and TypeScript service with its own `package.json` (no workspace), running Lighthouse and a same origin link crawl (depth 3, 200 pages, 10 second timeout, robots.txt respected, only 4xx, 5xx and timeouts reported). A Lighthouse result stores about 1 KB: five category scores, LCP, TBT, failed audit ids; the full report is discarded. Results are summaries, so the 30 day cleanup must not touch them. Fallback: a pending check unclaimed for `CHECK_CLAIM_WINDOW` (default 30 minutes) goes to the cloud PageSpeed path; a lapsed lease returns it to pending and the window applies again; **a posted result is terminal whatever it says.** Tests: fast unit tests with a faked HTTP side and stubbed Lighthouse in a new Node CI job, plus one real Lighthouse integration test against a locally served page in the compose job. A `checks/compose.yml` with Node and Chromium, restart `unless-stopped`, for the self hosted machine.
3. **Telegram webhook, chat, progress.** `POST /telegram` verifies the secret token header and the chat ID, maps free text to a task through the model with the registry as the schema, handles `/status`, `/runs`, `/cancel`. Progress is one message edited as events land. `tests/telegram_fake.py` is the fake server. Cold start measured with timed log lines on the live deploy and written into the README as a number.
4. **Approvals and repo chores.** `approvals` table, inline buttons, 24 hour expiry. `repo_chore` clones into a temp directory, branches `agent/<run id>`, runs the repo's tests, pushes, opens the PR with the GitHub token. No PR on red. Takeover checks the remote for the branch first. Integration test against `mercury-fixture`, marked `integration`.
5. **Digest, audits, cleanup.** `digest` type at 07:30 Melbourne. CI failure watch hourly through the Actions API. `pip-audit` and `npm audit` weekly with findings in the digest only. Cleanup task deletes event bodies older than 30 days and **must leave check summaries alone**, or the digest can never say a score moved. Write the test that proves a summary survives the cleanup.
6. **README claims and the runs page, as a React app.** `web/` is Vite, React and TypeScript, its own `package.json`, Node 22, compiled to static assets that **FastAPI serves at the root, not nginx** (the cloud deployment has no nginx, so serving from nginx would make local and production disagree). API routes take precedence; unmatched paths fall through to the page. The Dockerfile becomes multi stage: a Node stage builds `web/`, the Python stage copies the output, and the existing `COPY` list gains it. Both `ci.yml` and `deploy.yml` gain a Node job running typecheck and Vitest.

   The page lists runs from `GET /runs` and opens one to stream its events. The piece worth building carefully is a `useRunStream` hook holding an `EventSource` that merges events by their monotonic id, resumes with `Last-Event-ID` without duplicating rows, and drives the kill connection button. **No login and no token in the browser:** metadata for every run, full event bodies only for task types flagged public. Scope stops at the list, the detail view and the kill button; Telegram owns approvals and chat. Tests are Vitest and React Testing Library against a fake `EventSource`, covering the merge on reconnect and the duplicate id case.

   Paste test output under each of the seven new rows. Rename the repo to `mercury` on GitHub last, after everything is green, and update the README title, `CLAUDE.md` and the private repo's image reference in one commit.

## Guards that must exist before the webhook is registered

Chat ID allowlist of one. Secret token check. Per run, per day per provider, and per month caps in config. The bearer token on every non public endpoint. `MAX_RUNS_PER_DAY` still applied to the public `pytest` endpoint. The claim endpoint restricted to `site_check` of the declared kinds, with a test asserting it refuses every other type.

## What not to do

Do not merge or publish anything from a task. Do not add a paid resource; the Job and the webhook exist so nothing stays awake. Do not use long polling. Do not pin `latest` in the private repo. Do not mention my employer, its customers or its code anywhere. Do not write a README claim before its proof exists. Do not put a token in a commit, a log line or a Telegram message.

## When done

Push. The session on the other machine pulls, reads the README and updates its own records from the measured numbers.
