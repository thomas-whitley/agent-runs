# agent-runs

A FastAPI agent loop that streams its steps over SSE and resumes from the last event after a dropped connection. Run state lives in PostgreSQL, so more than one replica serves the same run. Deployed to Azure Container Apps at scale to zero by GitHub Actions.

Status: seed commit. The design is in `docs/spec.md` and the build order in `docs/build-brief.md`. The claims table below fills in as each test goes green and not before.

## Claims and their proof

| Claim | Test | Status |
|---|---|---|
| A client that drops mid run reconnects with `Last-Event-ID` and receives the remaining steps exactly once | `tests/test_resume.py` | pending |
| Two replicas serve one run; a reconnect to the other replica resumes correctly | `tests/test_two_replicas.py` | pending |
| A retried step that already committed is a no-op | `tests/test_idempotent.py` | pending |
| Retrieval over the corpus feeds the loop | `tests/test_retrieval.py` | pending |
| Deployed to Azure Container Apps by GitHub Actions with OIDC, traced end to end | `.github/workflows/deploy.yml` | pending |

## What the agent does

You post a pytest file. The agent writes the function that makes it pass, running pytest in a subprocess with a timeout and no network inside the worker container. That is the whole sandbox, and it is a demo sandbox, not a security boundary.

## Cost

Target $0 a month idle: Container Apps free grant at scale to zero, Supabase free plan, GitHub free tier. The only variable cost is model tokens during a demo, capped per run and per day.

## Licence

MIT.
