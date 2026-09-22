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

## Step order (from `docs/mercury.md`; each ends with its test green, then a commit)

1. **Task registry and provider per type.** `app/tasks.py` with a `TaskType` dataclass (name, tools, provider, budget, public) and the five entries. `runs.type` column by migration, default `pytest` for existing rows. `POST /runs` takes `type` and `inputs`; the worker picks the provider from the registry, not from `MODEL`. Config gains `PROVIDERS` as a mapping of name to base URL, key variable and model. `tests/test_tasks.py`. Existing tests still green.
2. **Scheduled Job and site checks.** `infra/main.bicep` gains a Container Apps Job, cron trigger, that runs the same image with `ROLE=scheduler` and posts the due tasks to the api with the bearer token. `site_check` type: HTTP status and latency for the URL, no model call. Schedule read from the mounted config. Log line `scheduled run <id> created with no client` and the first README claim green from it.
3. **Telegram webhook, chat, progress.** `POST /telegram` verifies the secret token header and the chat ID, maps free text to a task through the model with the registry as the schema, handles `/status`, `/runs`, `/cancel`. Progress is one message edited as events land. `tests/telegram_fake.py` is the fake server. Cold start measured with timed log lines on the live deploy and written into the README as a number.
4. **Approvals and repo chores.** `approvals` table, inline buttons, 24 hour expiry. `repo_chore` clones into a temp directory, branches `agent/<run id>`, runs the repo's tests, pushes, opens the PR with the GitHub token. No PR on red. Takeover checks the remote for the branch first. Integration test against `mercury-fixture`, marked `integration`.
5. **Digest, audits, cleanup.** `digest` type at 07:30 Melbourne. CI failure watch hourly through the Actions API. `pip-audit` and `npm audit` weekly with findings in the digest only. Cleanup task deletes event bodies older than 30 days.
6. **README claims and the runs page.** `static/index.html` grows into a list of runs with type, provider, status, tokens and duration, each opening its stream. Paste test output under each of the five new rows. Rename the repo to `mercury` on GitHub last, after everything is green, and update the README title, `CLAUDE.md` and the private repo's image reference in one commit.

## Guards that must exist before the webhook is registered

Chat ID allowlist of one. Secret token check. Per run, per day per provider, and per month caps in config. The bearer token on every non public endpoint. `MAX_RUNS_PER_DAY` still applied to the public `pytest` endpoint.

## What not to do

Do not merge or publish anything from a task. Do not add a paid resource; the Job and the webhook exist so nothing stays awake. Do not use long polling. Do not pin `latest` in the private repo. Do not mention my employer, its customers or its code anywhere. Do not write a README claim before its proof exists. Do not put a token in a commit, a log line or a Telegram message.

## When done

Push. The session on the other machine pulls, reads the README and updates its own records from the measured numbers.
