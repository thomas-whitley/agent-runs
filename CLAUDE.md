# agent-runs

A FastAPI agent loop that streams its steps over SSE and resumes after a dropped connection, with run state in PostgreSQL so more than one replica serves one run. Deployed to Azure Container Apps by GitHub Actions. The design is `docs/design.md`. Read it before doing anything.

## How to work here

- One change at a time. Each ends with its test green. Write the failing test first.
- The README has a claims table. A row is filled in only when its test passes. Never write a claim ahead of its test.
- `uv` for everything Python (`uv run pytest`, `uv run ruff check`). Lock file committed.
- CI must stay green on every push. The CI test loop uses the stub model, never a real API key.
- Commit after every green task with a plain message that says what now works. No emoji, no "feat:" prefixes.

## Hard rules

- Nothing from my employer, its codebase, customers or tickets appears in this repo, in any form. This is a personal project.
- No paid cloud resource without me saying yes in the conversation. Target is $0 a month idle: Container Apps free grant, Supabase free plan, GitHub free tier. No custom domain, no paid Postgres, no Redis, no static IP.
- Secrets live in GitHub Actions secrets and Container Apps secrets, never in the repo, never in a commit message, never in a log line. `.env` is gitignored.
- The agent's sandbox is a subprocess with a timeout and no network inside the worker container. The README says exactly that. Do not describe it as more than it is.
- Model defaults: Haiku 4.5, 50k tokens per run, 20 runs a day on the public URL. Config, not code.

## Writing rules (README, docs, commit messages)

- No em dashes, no en dashes. Use a comma, a full stop or brackets.
- Complete sentences. No colon lists in prose, no "X is the Y" kickers, no closing aphorisms.
- Numbers over adjectives. "Resumed from event 14 after a 3 second drop" beats "robust resume".
- Banned words: leverage, utilize, streamline, delve, harness, foster, unlock, robust, seamless, passionate, driven, innovative, cutting-edge, journey, landscape, testament, pivotal, elevate, enhance, empower, ecosystem, holistic.
- The README is the deliverable as much as the code. A reader should be able to run one command, kill their connection mid stream, reconnect, and see it resume, with the test output that proves it.
