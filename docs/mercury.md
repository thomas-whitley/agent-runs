# Mercury

Mercury is the second phase of this repo. agent-runs proved that an agent loop can stream its steps over SSE, survive a dropped connection on any replica, and keep its state in Postgres. Mercury turns that loop into an always available runner: it takes typed tasks from a queue, runs them on a schedule or on a message from a phone, and looks after a small portfolio of repos and one production website. The properties it adds are scheduling with scale to zero, budgets, approvals, and more than one model provider in one deploy. `docs/design.md` still describes the loop, the resume protocol and the data model; this file describes what sits on top.

## What stays the same

One image, one Postgres, N stateless API replicas, one worker. The worker still claims a run with a lease and writes one `steps` row and one `events` row per step in one transaction. A client still reads `GET /runs/{id}/events` and resumes with `Last-Event-ID`. Nothing about the resume protocol changes.

## Task types

A run has a type. The type registry is a table in code, one row per type, and each row names the tools the loop may call, the default provider and the token budget.

| Type | Input | Result | Default provider |
| --- | --- | --- | --- |
| `pytest` | a pytest file | a function that passes it | Gemini free tier |
| `chat` | one Telegram message plus the last 20 turns | one reply | Gemini free tier, Claude Haiku 4.5 selectable |
| `repo_chore` | a repo name and an instruction | a pull request | Gemini free tier, Claude Haiku 4.5 selectable |
| `site_check` | a URL | a report | none, no model call |
| `digest` | nothing | a summary of the last day's checks | Gemini free tier |

Nothing is added to this table until every row is green in the README.

## Execution

The loop is the same hand rolled plan, retrieve, act, verify. What changes is that act now dispatches to a typed tool registry, and the registry a run sees is the one its task type names. `chat` sees read only tools over the runner's own state (list runs, read a run's events, the portfolio status from the last checks) plus one write, create a task. It cannot touch a repo or the site. Every side effect goes through a typed task, so the autonomy ceiling and the budgets apply to all of them.

The provider is chosen per task type from config. A `Model` implementation exists for the stub (CI), any OpenAI compatible endpoint (Gemini's free tier is the default) and the Anthropic SDK. Two task types on two providers in one deploy is one of the README claims.

## Autonomy ceiling

The runner may open pull requests and write drafts. It never merges and never publishes. A merge can become an approval button later; it is not in this phase.

## Repo chores

A `repo_chore` clones the repo into a temporary directory inside the worker container, branches as `agent/<run id>`, makes the change, runs the repo's own tests, pushes the branch and opens a PR. If the tests fail after the change, the run ends `failed` with the diff and the test output in the final event, and no PR is opened. The user can say "open it anyway" from Telegram, which creates a new run that pushes the existing branch.

Auth is a fine grained GitHub personal access token in Container Apps secrets, scoped to the named repos, contents and pull requests only, one year expiry. It cannot push to `main` because branch protection on each repo forbids it, and the runner never tries.

Takeover: the worker heartbeats every 30 seconds and the lease is two minutes. A replica that finds an expired lease takes the run over. The clone was local to the dying container, so the chore restarts from the beginning. That is safe because the first step checks whether `agent/<run id>` already exists on the remote and resumes from it if so.

## Scheduling

A Container Apps Job with a cron trigger posts to the API. It is declared in the same Bicep as the apps, so the deploy workflow shows it, and it runs inside the same free grant. The API and the worker still scale to zero between runs; the Job only wakes them when there is work. An in process scheduler was rejected because it needs a replica awake all month, which alone would cost about 3.6 times the free grant. `pg_cron` was rejected because it would tie the schedule to the database plan.

The default schedule: site uptime hourly (plain HTTP, no model call), CI failure watch hourly (GitHub Actions API for the named repos), Lighthouse and broken links weekly, dependency audit weekly per repo (`pip-audit` or `uv` for Python, `npm audit` for Node), stale PR and issue triage weekly, and the digest daily. A cleanup task runs on the same Job and deletes event bodies older than 30 days. Run rows and check summaries are kept a year, which is what makes a week over week comparison in the digest possible.

## Two executors for one task type

The checks that need a real browser run somewhere else. Lighthouse and Playwright are Node libraries, and putting Chromium in the API image would cost image size and cold start for a check that runs once a week. So `site_check` has two executors and the runner picks whichever is available.

`checks/` is a small Node service, in this repo, that runs on a self hosted machine. It declares which check kinds it can run, claims one pending check at a time, runs it locally, and posts the result back. It talks to the API over HTTPS with a bearer token and never holds a database credential, so the machine it runs on holds exactly one secret. It owns Lighthouse and the broken link crawl, weekly. Uptime and the CI failure watch stay in the cloud on the hourly Job, because they are plain HTTP and should not depend on a machine that sleeps.

Three endpoints serve it, all behind the bearer token: claim one pending check, extend its lease, post its result. Claim only ever returns a `site_check` of the kinds the worker declared. It cannot return a `pytest`, `chat`, `repo_chore` or `digest` run, so the token on the self hosted machine is a strictly weaker credential than the one a session uses, by construction rather than by convention.

When the machine is asleep the work does not stall. A pending check that nobody claims within a configurable window, thirty minutes by default, is taken by the cloud executor over the PageSpeed Insights API. A claimed check whose lease expires goes back to pending under the same two minute lease the Python worker uses, and the same window then applies to it. A check that comes back with a verdict is finished, whatever the verdict is: a result saying the site returned 500 is a successful check with a bad finding, not a reason to run it again somewhere else.

A Lighthouse result is stored as about a kilobyte: the five category scores, largest contentful paint, total blocking time, and the ids of the audits that failed. The full report is discarded. The crawl is same origin only, depth three, two hundred pages, ten second request timeout, robots.txt respected, and it reports only 4xx, 5xx and timeouts. Which executor ran a check is recorded in its own column, separate from the model provider, so the provider column keeps one meaning.

## Telegram

The bot receives updates by webhook, not long polling, for the same reason the scheduler is a Job: polling needs a replica awake. The first message after idle pays a cold start of a few seconds, and the README states the measured figure.

Inbound: the webhook checks Telegram's secret token header and then the chat ID against an allowlist of one. Every other sender gets no reply at all.

Free text goes to the model with the task registry as a structured output schema. The model either picks a type with its inputs filled or asks one clarifying question. `/status`, `/runs` and `/cancel` bypass the model. A `repo_chore` always echoes the repo and the instruction and waits for a button press before it starts; every other type starts at once.

Progress is one message edited in place as SSE events land, not one message per step. Approvals are inline keyboard buttons. Each pending approval is a row with the action, the run, an expiry and the Telegram message ID; the callback carries the row ID. An approval nobody answers expires after 24 hours and its run ends `cancelled`.

Conversation memory is a bounded window of the last 20 turns per chat in Postgres, plus the retrieval corpus already in the loop.

A failed scheduled task sends one message. The same task failing again within 24 hours goes in the digest instead, so a broken site does not page every hour.

## Budgets

Three caps, all config: tokens per run (default 50,000), tokens per day per provider (default 500,000), dollars per month. A tripped cap ends the run with one `budget` event and one Telegram message. The Azure budget alert at $5 a month stays as the outer guard.

## Configuration

The public repo ships the image and a sample config in `config/mercury.sample.yaml`. A private repo holds the real config: task definitions, schedules, the chat ID, the list of repos and the site URL, plus a GitHub Actions workflow that deploys the public image with that config. Secrets live in that repo's Actions secrets and in Container Apps secrets. The private repo pins an image tag and the tag is bumped by hand, so a public commit cannot change what talks to the phone before its owner has read it.

## The runs page

`web/` is a Vite, React and TypeScript app compiled to static assets and served by the API at the root. It lists runs with type, provider, executor, status, tokens and duration, newest first, and opening one streams its events live. The interesting part is a `useRunStream` hook that holds an `EventSource`, merges arriving events into existing state by their monotonic id, survives a reconnect with `Last-Event-ID` without duplicating rows, and drives the kill connection button that lets a visitor drop the socket and watch the resume happen. Its scope stops there: no approvals and no chat view, because Telegram owns both.

The API serves the bundle rather than nginx. The nginx in the compose file exists only so the two replica test has one port, and the cloud deployment runs the API directly, so serving from nginx would make local and production disagree about the one thing the page demonstrates. API routes take precedence and unmatched paths fall through to the page.

`GET /runs` is newest first with keyset pagination on the created timestamp and id, fifty rows by default and two hundred at most, counted against the same rate limit as the other public endpoints. Keyset rather than offset, because run rows are kept a year and an offset scan degrades and skips rows as new runs arrive.

The image is built in two stages: a Node stage compiles `web/`, and the Python stage copies the output. `web/` and `checks/` are separate npm projects that import nothing from each other, both on Node 22, pinned in the build stage and in CI.

## What is public

Run metadata (type, provider, executor, status, tokens, duration) is public and shows on the runs page. Event bodies are behind a bearer token. A `public` flag per task type lets `pytest` demo runs show in full. The browser never authenticates: it shows metadata for every run and full events only for public task types, which is why there is no login and no token in any page. Sessions and other clients authenticate with one static bearer token from secrets, rotated by redeploy. Rate limits on the public endpoints are unchanged.

## Tests

Telegram is a fake HTTP server in the test suite that records sent messages and replays button callbacks. GitHub is a public throwaway repo, `mercury-fixture`, that one integration test runs a real chore against, opening and then closing a PR. Python unit tests never touch the network. `web/` is tested with Vitest and React Testing Library against a fake `EventSource`, covering the merge on reconnect and the duplicate id case. `checks/` has fast unit tests with a faked HTTP side and a stubbed Lighthouse call, plus one integration test that runs a real Lighthouse against a locally served page. Everything marked `integration` runs only in the compose job.

## Claims for this phase

Seven rows join the README's claims table. Each goes green only when a test or a measured log line proves it.

| Claim | Proof |
| --- | --- |
| A scheduled task runs with no client connected | Job log line plus the run row |
| A Telegram message opens a PR on a named repo | integration test against `mercury-fixture` |
| The webhook cold start is measured and stated | timed log lines from the live deploy |
| A budget trip ends a run with one event and one message | unit test with the fake Telegram |
| Two task types run on two providers in one deploy | run rows with two provider names |
| A dropped browser stream resumes from the last event without duplicating rows | Vitest test against a fake `EventSource`, and the live page |
| A weekly check runs on a self hosted worker and falls back to the cloud path when it is offline | integration test exercising the fallback window |

## Build order

1. Task registry and provider per type, plus `GET /runs` and its serializer. `pytest` keeps working.
2. Scheduled Job in Bicep, the `site_check` type, the three claim endpoints and the `checks/` worker. First claim green.
3. Telegram webhook, `chat`, progress messages. Cold start measured.
4. Approvals table and `repo_chore`, against `mercury-fixture`.
5. `digest`, audits, cleanup.
6. README claims and the runs page in `web/`.

Each step shows something from the phone before the riskiest piece, repo chores, lands. The run list endpoint is in step 1 rather than step 6 because the chat tools and `curl` both want it long before a page does.

## Out of scope

Merging or publishing anything. More than one user. A GitHub App (the install flow and JWT exchange buy nothing here). Full conversation history with embeddings. Any browser UI beyond the runs page, and any authentication in the browser.
