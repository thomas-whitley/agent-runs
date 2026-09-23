# Mercury Step 2a: Scheduler and Cloud-Side Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Container Apps Job (`ROLE=scheduler`) that wakes on a cron trigger, checks each configured site's plain HTTP status and latency with no browser and no model call, creates and closes each run itself through the API with a bearer token, and suspends a site's checks after three consecutive failures until something resumes it.

**Architecture:** The scheduler is a third role of the existing single image, alongside `api` and `worker`. It has its own entry point (`app/scheduler.py`) and its own Container Apps Job resource in the Bicep, on the same free grant. It reads `portfolio.sites` from a mounted `mercury.yaml`, and for each site not currently suspended: POSTs `type=site_check` to `POST /runs` (proving "no client" with a log line), performs the HTTP check itself in the same process (no claim/lease round trip — that mechanism is for the Lighthouse worker in step 2b/2c, not this), writes the result directly to the database it already holds a connection to, and updates a small per-schedule failure counter. `POST /runs` gains a bearer-token guard for every non-public task type, which `pytest` is exempt from.

**Tech Stack:** Python 3.12, FastAPI, psycopg3, PyYAML (new dependency), stdlib `urllib` for the outbound HTTP the scheduler makes (no new HTTP client dependency), pytest, Bicep (Container Apps Jobs).

**Spec:** `docs/build-brief-mercury.md` (the "2a" bullet under "Step order") and `docs/mercury.md` ("Scheduling" and "Two executors for one task type" sections). Both travel with this plan; read them before Task 1.

## Global Constraints

- One change at a time. Each task ends with its own tests green, commit before starting the next. (`CLAUDE.md`)
- Write the failing test first for every behavioral task. (`CLAUDE.md`)
- `uv` for everything Python: `uv add`, `uv run pytest`, `uv run ruff check .`. (`CLAUDE.md`)
- No em dashes, no en dashes, no banned words (leverage, utilize, streamline, delve, harness, foster, unlock, robust, seamless, passionate, driven, innovative, cutting-edge, journey, landscape, testament, pivotal, elevate, enhance, empower, ecosystem, holistic) in commit messages or docs. (`CLAUDE.md`)
- Commit messages: plain, say what now works, no emoji, no "feat:" prefix. (`CLAUDE.md`)
- Nothing from the user's employer ever appears in this repo. No paid cloud resource without the user saying yes in conversation — the Container Apps Job stays inside the existing free grant (min/max replicas implicit at 1 run per trigger, consumption plan, same environment as the existing apps). (`CLAUDE.md`)
- CI must stay green using `MODEL=stub`; nothing in this plan calls a real model (`site_check` has `provider: None` already). (`CLAUDE.md`)
- Every migration is additive and nullable/defaulted, never a `NOT NULL` column with no default on an existing table, because rolling deploys depend on it. (`docs/handoff.md`'s carried-forward rule, still true.)
- Claim must never return a `pytest`, `chat`, `repo_chore`, or `digest` run — not exercised by this plan (claim endpoints are step 2b), but nothing here should make that harder to build. (`docs/build-brief-mercury.md`)
- The live-deploy portion of 2a's exit condition (the log line from the real deployment, and README claim one) cannot close in this session: `MERCURY_BEARER_TOKEN` does not exist in this repo's secrets yet. Every task below is scoped to what is provable locally; the plan says explicitly, at the end, what is deferred and why.

---

## Task 1: mercury.yaml config loading (just what 2a reads)

**Files:**
- Create: `app/mercury_config.py`
- Test: `tests/test_mercury_config.py`
- Modify: `pyproject.toml` (add `pyyaml` dependency)

**Interfaces:**
- Produces: `MercuryConfig` (frozen dataclass, field `sites: tuple[str, ...]`) and `load_mercury_config(path: str | pathlib.Path) -> MercuryConfig`, both imported by Task 5's `app/scheduler.py`.

- [ ] **Step 1: Add the dependency**

```bash
uv add pyyaml
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_mercury_config.py
"""Loads only what step 2a reads from mercury.yaml: the portfolio's sites.

The full schema (providers, tasks, budgets, telegram, retention) belongs to
later steps; parsing sections nothing calls yet is code with no caller.
"""

import pytest
import yaml

from app.mercury_config import load_mercury_config


def test_load_mercury_config_reads_the_site_list(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(
        yaml.dump({"portfolio": {"sites": ["https://a.example", "https://b.example"]}})
    )

    config = load_mercury_config(config_file)

    assert config.sites == ("https://a.example", "https://b.example")


def test_load_mercury_config_defaults_to_no_sites_when_the_section_is_missing(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(yaml.dump({"telegram": {"chat_id": 1}}))

    config = load_mercury_config(config_file)

    assert config.sites == ()


def test_load_mercury_config_raises_when_the_file_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_mercury_config(tmp_path / "does-not-exist.yaml")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mercury_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mercury_config'`

- [ ] **Step 4: Write the implementation**

```python
# app/mercury_config.py
"""Loads the parts of mercury.yaml step 2a actually reads: the portfolio's
sites. The full schema arrives with the steps that read the rest of it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MercuryConfig:
    sites: tuple[str, ...]


def load_mercury_config(path: str | Path) -> MercuryConfig:
    """Read mercury.yaml. Raises FileNotFoundError if it is not there,
    which is what a scheduler started with no config mounted should do."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    portfolio = data.get("portfolio") or {}
    return MercuryConfig(sites=tuple(portfolio.get("sites") or []))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mercury_config.py -v`
Expected: 3 passed

- [ ] **Step 6: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 7: Commit**

```bash
git add app/mercury_config.py tests/test_mercury_config.py pyproject.toml uv.lock
git commit -m "Read the portfolio's site list from mercury.yaml

Only portfolio.sites, which is what the scheduler needs for step 2a.
The rest of mercury.yaml's schema is parsed by whichever later step
first has a caller for it."
```

---

## Task 2: The bearer token guard on non-public task types

**Files:**
- Modify: `app/config.py` (add `mercury_bearer_token: str | None` to `Settings` and `load_settings`)
- Create: `app/auth.py`
- Modify: `app/main.py` (call the guard in `create_run` when the type is not public)
- Modify: `tests/test_runs.py` (new tests)
- Modify: `tests/test_worker.py`, `tests/test_retrieval.py` (their `Settings(...)` helpers gain the new required field)

**Interfaces:**
- Consumes: `TASK_TYPES[run.type].public` (already exists, `app/tasks.py`).
- Produces: `require_bearer_token(request: fastapi.Request) -> None` (raises `HTTPException(401)`), imported by `app/main.py` and, later, by the claim endpoints in step 2b.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_runs.py
def test_post_runs_requires_a_bearer_token_for_a_non_public_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "digest", "inputs": {"task": "x"}})

    assert response.status_code == 401


def test_post_runs_accepts_the_right_bearer_token_for_a_non_public_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "x"}},
        headers={"Authorization": "Bearer the-real-token"},
    )

    assert response.status_code == 201


def test_post_runs_rejects_the_wrong_bearer_token(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(
        f"{base_url}/runs",
        json={"type": "digest", "inputs": {"task": "x"}},
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_post_runs_never_requires_a_token_for_the_public_pytest_type(start_server, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", "the-real-token")
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "pytest", "inputs": {"task": "x"}})

    assert response.status_code == 201


def test_post_runs_refuses_a_non_public_type_when_no_token_is_configured(start_server):
    """Fail closed: an unset MERCURY_BEARER_TOKEN must not wave every caller through."""
    base_url = start_server()

    response = httpx2.post(f"{base_url}/runs", json={"type": "digest", "inputs": {"task": "x"}})

    assert response.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runs.py -v -k bearer_token_or_token`
Expected: FAIL — every new test gets `201`/no 401 today, because nothing checks a token yet (or a 500 if `Settings` construction errors, whichever comes first once Step 3 below lands the field before the guard is wired — run this before touching `config.py` so the failure is about missing behavior, not a missing field).

- [ ] **Step 3: Add the setting**

In `app/config.py`, add to `Settings`:

```python
    mercury_bearer_token: str | None
```

placed after `voyage_api_key: str | None` (keeps related "optional secret" fields together). In `load_settings()`:

```python
        mercury_bearer_token=os.environ.get("MERCURY_BEARER_TOKEN") or None,
```

placed after the `voyage_api_key=` line.

- [ ] **Step 4: Write the guard**

```python
# app/auth.py
"""Bearer token check for every non-public endpoint, per docs/mercury.md's
guard list: "The bearer token on every non public endpoint."
"""

from fastapi import HTTPException, Request


def require_bearer_token(request: Request) -> None:
    """Raise 401 unless Authorization: Bearer <token> matches the configured
    one. An unconfigured token refuses every caller rather than waving them
    through, so a deploy that forgot MERCURY_BEARER_TOKEN fails closed."""
    expected = request.app.state.settings.mercury_bearer_token
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if not expected or scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
```

- [ ] **Step 5: Wire it into `create_run`**

In `app/main.py`, add the import:

```python
from app.auth import require_bearer_token
```

In `create_run`, right after `task_type = TASK_TYPES[run.type]`:

```python
        if not task_type.public:
            require_bearer_token(request)
```

- [ ] **Step 6: Update the two `Settings(...)` test helpers**

In `tests/test_worker.py`'s `settings_with` and `tests/test_retrieval.py`'s `settings_for_retrieval`, add `mercury_bearer_token=None,` to the `defaults` dict (any line inside the dict; keep it near `voyage_api_key` for readability).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_runs.py tests/test_worker.py tests/test_retrieval.py -v`
Expected: all pass, including the 5 new ones.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (this must not break any existing `POST /runs` test, since `pytest` stays public).

- [ ] **Step 9: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 10: Commit**

```bash
git add app/config.py app/auth.py app/main.py tests/test_runs.py tests/test_worker.py tests/test_retrieval.py
git commit -m "Guard every non-public task type behind a bearer token

POST /runs now requires Authorization: Bearer <MERCURY_BEARER_TOKEN> for
every type except pytest, which stays public. An unset token refuses the
request rather than allowing it, so a deploy that forgot to set it fails
closed instead of quietly exposing every task type. This is what the
scheduler in the next commits authenticates with."
```

---

## Task 3: Per-schedule suspension after three consecutive failures

**Files:**
- Create: `migrations/006_schedule_state.sql`
- Create: `app/schedule_state.py`
- Test: `tests/test_schedule_state.py`

**Interfaces:**
- Produces: `is_suspended(conn, name) -> bool`, `record_failure(conn, name) -> None`, `record_success(conn, name) -> None`, `resume(conn, name) -> None`, all imported by Task 5's `app/scheduler.py`. `name` is a plain string; Task 5 uses `f"site_uptime:{url}"` so one flaky site does not suspend checks on the others sharing the same schedule entry.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_schedule_state.py
"""Three consecutive failures suspends one schedule entry until something
resumes it, so a broken site does not retry every hour forever and burn
the workspace's daily log ingestion cap.
"""

from app.schedule_state import is_suspended, record_failure, record_success, resume

NAME = "site_uptime:https://a.example"


def test_a_schedule_is_not_suspended_before_any_failure(migrated_db):
    assert is_suspended(migrated_db, NAME) is False


def test_three_consecutive_failures_suspends(migrated_db):
    record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is False
    record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is False
    record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True


def test_a_success_resets_the_streak_before_suspension(migrated_db):
    record_failure(migrated_db, NAME)
    record_failure(migrated_db, NAME)
    record_success(migrated_db, NAME)
    record_failure(migrated_db, NAME)
    record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is False


def test_resume_clears_a_suspension(migrated_db):
    for _ in range(3):
        record_failure(migrated_db, NAME)
    assert is_suspended(migrated_db, NAME) is True

    resume(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is False


def test_a_success_after_suspension_does_not_auto_resume(migrated_db):
    """Only an explicit resume clears it, so a flapping site cannot self heal silently."""
    for _ in range(3):
        record_failure(migrated_db, NAME)

    record_success(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True


def test_two_schedule_names_are_independent(migrated_db):
    other = "site_uptime:https://b.example"
    for _ in range(3):
        record_failure(migrated_db, NAME)

    assert is_suspended(migrated_db, NAME) is True
    assert is_suspended(migrated_db, other) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schedule_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schedule_state'`

- [ ] **Step 3: Write the migration**

```sql
-- migrations/006_schedule_state.sql
-- Tracks consecutive failures per named schedule entry (one row per site
-- under site_uptime, for example), so a flaky check suspends on its own
-- without silencing every other schedule entry.
CREATE TABLE schedule_state (
    name                 text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0,
    suspended            boolean NOT NULL DEFAULT false,
    suspended_at         timestamptz
);
```

- [ ] **Step 4: Write the implementation**

```python
# app/schedule_state.py
"""Per-schedule failure tracking. Three consecutive failures suspends a
schedule entry until something resumes it, so a broken site does not retry
every hour forever and burn the workspace's daily log ingestion cap.
"""

import psycopg

SUSPEND_AFTER = 3

_ENSURE_ROW = "INSERT INTO schedule_state (name) VALUES (%s) ON CONFLICT (name) DO NOTHING"

_READ_SUSPENDED = "SELECT suspended FROM schedule_state WHERE name = %s"

_RECORD_FAILURE = """
UPDATE schedule_state
SET consecutive_failures = consecutive_failures + 1,
    suspended = (consecutive_failures + 1) >= %s,
    suspended_at = CASE
        WHEN (consecutive_failures + 1) >= %s THEN now()
        ELSE suspended_at
    END
WHERE name = %s
"""

_RECORD_SUCCESS = """
UPDATE schedule_state SET consecutive_failures = 0
WHERE name = %s AND suspended = false
"""

_RESUME = """
UPDATE schedule_state
SET suspended = false, consecutive_failures = 0, suspended_at = NULL
WHERE name = %s
"""


def is_suspended(conn: psycopg.Connection, name: str) -> bool:
    conn.execute(_ENSURE_ROW, (name,))
    row = conn.execute(_READ_SUSPENDED, (name,)).fetchone()
    return bool(row[0]) if row else False


def record_failure(conn: psycopg.Connection, name: str) -> None:
    conn.execute(_ENSURE_ROW, (name,))
    conn.execute(_RECORD_FAILURE, (SUSPEND_AFTER, SUSPEND_AFTER, name))


def record_success(conn: psycopg.Connection, name: str) -> None:
    conn.execute(_ENSURE_ROW, (name,))
    conn.execute(_RECORD_SUCCESS, (name,))


def resume(conn: psycopg.Connection, name: str) -> None:
    conn.execute(_ENSURE_ROW, (name,))
    conn.execute(_RESUME, (name,))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_schedule_state.py -v`
Expected: 6 passed

- [ ] **Step 6: Run the migrations test too**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: still passes (the new migration file applies cleanly; `CORE_TABLES` in that test does not need `schedule_state` added, since that set only asserts the four tables that predate task types).

- [ ] **Step 7: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 8: Commit**

```bash
git add migrations/006_schedule_state.sql app/schedule_state.py tests/test_schedule_state.py
git commit -m "Add per-schedule suspension after three consecutive failures

schedule_state is one row per named schedule entry (site_uptime:<url>, so
one flaky site does not silence the others under the same schedule name).
record_failure suspends on the third consecutive one; only an explicit
resume clears it, not a later success, so a flapping site cannot self heal
silently and page again on the next failure. Nothing calls this yet; the
scheduler does, in a later commit."
```

---

## Task 4: The cloud side check itself

**Files:**
- Create: `app/checks.py`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces: `SiteCheckResult` (frozen dataclass: `url: str, status_code: int | None, latency_ms: float | None, passed: bool, error: str | None = None`) and `check_site(url: str, timeout_seconds: float = 10.0) -> SiteCheckResult`, imported by Task 5's `app/scheduler.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_checks.py
"""The cloud side check: plain HTTP status and latency, no browser, no
model call. A bad result is data, never an exception — the caller always
gets a SiteCheckResult back.
"""

import http.server
import threading
import time

import pytest

from app.checks import check_site


@pytest.fixture
def local_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                time.sleep(0.3)
            if self.path == "/broken":
                self.send_response(500)
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def test_check_site_passes_on_a_2xx_response(local_server):
    result = check_site(local_server)

    assert result.passed is True
    assert result.status_code == 200
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.error is None


def test_check_site_fails_on_a_5xx_response(local_server):
    result = check_site(f"{local_server}/broken")

    assert result.passed is False
    assert result.status_code == 500


def test_check_site_fails_and_does_not_raise_on_a_timeout(local_server):
    result = check_site(f"{local_server}/slow", timeout_seconds=0.05)

    assert result.passed is False
    assert result.status_code is None
    assert result.error


def test_check_site_fails_and_does_not_raise_on_an_unreachable_host():
    result = check_site("http://127.0.0.1:1", timeout_seconds=1.0)

    assert result.passed is False
    assert result.error
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_checks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.checks'`

- [ ] **Step 3: Write the implementation**

```python
# app/checks.py
"""The cloud side check: plain HTTP status and latency, no browser, no
model call. Uptime and the CI failure watch stay here rather than on the
self hosted worker, per docs/mercury.md, because they should not depend on
a machine that sleeps.
"""

import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class SiteCheckResult:
    url: str
    status_code: int | None
    latency_ms: float | None
    passed: bool
    error: str | None = None


def check_site(url: str, timeout_seconds: float = 10.0) -> SiteCheckResult:
    """A 2xx or 3xx within the timeout passes. Anything else, including a
    timeout or a connection error, is a finding, not an exception."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return SiteCheckResult(
                url=url,
                status_code=response.status,
                latency_ms=(time.monotonic() - started) * 1000,
                passed=response.status < 400,
            )
    except urllib.error.HTTPError as error:
        return SiteCheckResult(
            url=url,
            status_code=error.code,
            latency_ms=(time.monotonic() - started) * 1000,
            passed=error.code < 400,
        )
    except (urllib.error.URLError, OSError) as error:
        return SiteCheckResult(
            url=url,
            status_code=None,
            latency_ms=(time.monotonic() - started) * 1000,
            passed=False,
            error=str(error),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_checks.py -v`
Expected: 4 passed

- [ ] **Step 5: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 6: Commit**

```bash
git add app/checks.py tests/test_checks.py
git commit -m "Add the plain HTTP site check: status and latency, no browser

check_site never raises; a timeout, a connection error, and a 5xx all come
back as a SiteCheckResult with passed=False, because the caller (the
scheduler) needs a result to write and a failure count to update either
way, not an exception to catch."
```

---

## Task 5: The scheduler itself

**Files:**
- Modify: `app/config.py` (add `api_base_url: str` and `mercury_config_path: str` to `Settings`)
- Create: `app/scheduler.py`
- Test: `tests/test_scheduler.py`
- Modify: `tests/test_worker.py`, `tests/test_retrieval.py` (`Settings(...)` helpers gain the two new fields)

**Interfaces:**
- Consumes: `MercuryConfig.sites` (Task 1), `is_suspended`/`record_failure`/`record_success` (Task 3), `check_site`/`SiteCheckResult` (Task 4), `app.runs.record_step`/`finish_run` (existing), `Settings.mercury_bearer_token` (Task 2).
- Produces: `run_due_checks(conn, sites, api_base_url, bearer_token) -> list[str]` and `main()`, the `ROLE=scheduler` process entry point.

- [ ] **Step 1: Add the two settings**

In `app/config.py`, add to `Settings` (after `mercury_bearer_token`):

```python
    api_base_url: str
    mercury_config_path: str
```

In `load_settings()` (after `mercury_bearer_token=`):

```python
        api_base_url=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        mercury_config_path=os.environ.get("MERCURY_CONFIG_PATH", "/config/mercury.yaml"),
```

Add the same two fields (`api_base_url="http://localhost:8000",` and `mercury_config_path="/config/mercury.yaml",`) to the `defaults` dict in both `tests/test_worker.py`'s `settings_with` and `tests/test_retrieval.py`'s `settings_for_retrieval`.

- [ ] **Step 2: Run the existing suite to confirm the field addition alone breaks nothing**

Run: `uv run pytest -q`
Expected: all pass (this is purely additive to `Settings`).

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_scheduler.py
"""The scheduler: creates a site_check run through the API with the bearer
token, checks the site itself in the same process, writes the result, and
tracks consecutive failures per schedule entry.
"""

import httpx2

from app.scheduler import run_due_checks

BEARER_TOKEN = "test-bearer-token"


def test_run_due_checks_creates_a_run_and_writes_its_result(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()

    created = run_due_checks(migrated_db, (f"{base_url}/health",), base_url, BEARER_TOKEN)

    assert len(created) == 1
    run_id = created[0]
    row = migrated_db.execute(
        "SELECT type, status, tokens_used FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert row == ("site_check", "succeeded", 0)

    output = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'check'", (run_id,)
    ).fetchone()[0]
    assert output["passed"] is True
    assert output["status_code"] == 200


def test_run_due_checks_records_a_failed_check(start_server, migrated_db, monkeypatch):
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()

    created = run_due_checks(
        migrated_db, ("http://127.0.0.1:1",), base_url, BEARER_TOKEN
    )

    run_id = created[0]
    status = migrated_db.execute(
        "SELECT status FROM runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status == "failed"


def test_run_due_checks_skips_a_suspended_site(start_server, migrated_db, monkeypatch):
    from app.schedule_state import record_failure

    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    url = "http://127.0.0.1:1"
    for _ in range(3):
        record_failure(migrated_db, f"site_uptime:{url}")

    created = run_due_checks(migrated_db, (url,), base_url, BEARER_TOKEN)

    assert created == []


def test_run_due_checks_suspends_after_the_third_consecutive_failure(
    start_server, migrated_db, monkeypatch
):
    from app.schedule_state import is_suspended

    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()
    url = "http://127.0.0.1:1"

    run_due_checks(migrated_db, (url,), base_url, BEARER_TOKEN)
    run_due_checks(migrated_db, (url,), base_url, BEARER_TOKEN)
    assert is_suspended(migrated_db, f"site_uptime:{url}") is False

    run_due_checks(migrated_db, (url,), base_url, BEARER_TOKEN)

    assert is_suspended(migrated_db, f"site_uptime:{url}") is True


def test_run_due_checks_logs_the_created_run_with_no_client(
    start_server, migrated_db, monkeypatch, caplog
):
    """The exact line the live deploy's exit condition greps for."""
    monkeypatch.setenv("MERCURY_BEARER_TOKEN", BEARER_TOKEN)
    base_url = start_server()

    with caplog.at_level("INFO", logger="agent_runs.scheduler"):
        created = run_due_checks(migrated_db, (f"{base_url}/health",), base_url, BEARER_TOKEN)

    run_id = created[0]
    assert f"scheduled run {run_id} created with no client" in caplog.text
```

Note: `f"{base_url}/health"` is used as the checked "site" because it is a real endpoint the running test server actually answers with 200, which is what proves the check itself works end to end without standing up a second server. `httpx2` is imported for parity with the rest of the test suite even though this file does not call it directly beyond what `run_due_checks` does internally; drop the import if `ruff` flags it unused.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.scheduler'`

- [ ] **Step 5: Write the implementation**

```python
# app/scheduler.py
"""ROLE=scheduler: a Container Apps Job that wakes on a cron trigger, checks
the configured sites, and exits. It creates each run through the API with
the bearer token (proving no client is attached to watch it), then executes
and closes the run itself in the same process, because a plain HTTP check
needs no browser and no separate worker to wake up for it. Claim/heartbeat
endpoints exist for the self hosted Lighthouse worker in a later step, not
for this.
"""

import json
import logging
import urllib.error
import urllib.request

import psycopg
from opentelemetry import trace

from app.checks import check_site
from app.config import load_settings
from app.logging_setup import configure_logging
from app.mercury_config import load_mercury_config
from app.migrations import apply_migrations
from app.runs import finish_run, record_step
from app.schedule_state import is_suspended, record_failure, record_success
from app.telemetry import configure_telemetry

logger = logging.getLogger("agent_runs.scheduler")


def _create_run(api_base_url: str, bearer_token: str, url: str, timeout_seconds: float = 10.0) -> str:
    body = json.dumps({"type": "site_check", "inputs": {"task": url}}).encode()
    request = urllib.request.Request(
        f"{api_base_url}/runs",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())["id"]


def run_due_checks(
    conn: psycopg.Connection,
    sites: tuple[str, ...],
    api_base_url: str,
    bearer_token: str,
) -> list[str]:
    """Check every configured site not currently suspended. Returns the ids
    of the runs created, so a caller (or a test) can inspect them."""
    created: list[str] = []

    for url in sites:
        schedule_name = f"site_uptime:{url}"
        if is_suspended(conn, schedule_name):
            logger.info("skipping %s: schedule suspended", schedule_name)
            continue

        try:
            run_id = _create_run(api_base_url, bearer_token, url)
        except urllib.error.URLError:
            logger.exception("could not create a run for %s, skipping this cycle", url)
            continue

        logger.info(
            "scheduled run %s created with no client", run_id, extra={"run_id": run_id}
        )
        created.append(run_id)

        result = check_site(url)
        record_step(
            conn,
            run_id,
            1,
            "check",
            output={
                "status_code": result.status_code,
                "latency_ms": result.latency_ms,
                "passed": result.passed,
                "error": result.error,
            },
        )
        finish_run(conn, run_id, "succeeded" if result.passed else "failed", 0)

        if result.passed:
            record_success(conn, schedule_name)
        else:
            record_failure(conn, schedule_name)

    return created


def main() -> None:  # pragma: no cover - the process entry point
    configure_logging()
    settings = load_settings()
    apply_migrations(settings.database_url)
    tracing_on = configure_telemetry()

    if not settings.mercury_bearer_token:
        raise RuntimeError("no MERCURY_BEARER_TOKEN: the scheduler cannot call the api")

    config = load_mercury_config(settings.mercury_config_path)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        created = run_due_checks(
            conn, config.sites, settings.api_base_url, settings.mercury_bearer_token
        )

    logger.info("scheduler run complete, %s check(s) created", len(created))

    # The container exits within seconds of this returning. A batch span
    # processor's queue would be dropped unsent without an explicit flush.
    if tracing_on:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: 5 passed

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 8: Lint and format**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 9: Commit**

```bash
git add app/config.py app/scheduler.py tests/test_scheduler.py tests/test_worker.py tests/test_retrieval.py
git commit -m "Add the scheduler: creates and closes a site_check run itself

app/scheduler.py is ROLE=scheduler's entry point. For each configured site
not suspended, it posts to POST /runs with the bearer token, logs
'scheduled run <id> created with no client' (the log line the live deploy's
exit condition proves), performs the plain HTTP check in the same process,
writes the result, and updates that site's consecutive failure count. Spans
are flushed explicitly before the process exits, because a Job's container
does not live long enough for a batch export to happen on its own."
```

---

## Task 6: Wire the role into the image

**Files:**
- Modify: `docker/entrypoint.sh` (add the `scheduler` case, decode a mounted config secret)
- Modify: `docker-compose.yml` (a `scheduler` service for local manual verification, not part of the automated test suite)
- Modify: `.env.example` (document `MERCURY_BEARER_TOKEN`, `API_BASE_URL`, `MERCURY_CONFIG_PATH`)

**Interfaces:** None — this task wires existing pieces (Tasks 1-5) into the container. No new Python.

- [ ] **Step 1: Add the scheduler case and config decoding to the entrypoint**

```sh
#!/bin/sh
set -eu

ROLE="${ROLE:-api}"

if [ -n "${MERCURY_CONFIG_B64:-}" ]; then
  mkdir -p /config
  echo "$MERCURY_CONFIG_B64" | base64 -d > /config/mercury.yaml
fi

case "$ROLE" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m app.worker
    ;;
  scheduler)
    exec python -m app.scheduler
    ;;
  *)
    echo "Unknown ROLE '$ROLE'. Expected api, worker, or scheduler." >&2
    exit 1
    ;;
esac
```

(Full replacement of `docker/entrypoint.sh`.)

- [ ] **Step 2: Add a scheduler service to compose, for manual verification only**

Add to `docker-compose.yml`, as a sibling of the `worker` service:

```yaml
  scheduler:
    build: .
    profiles: ["scheduler"]  # docker compose run --rm scheduler, not part of `up`
    environment:
      ROLE: scheduler
      DATABASE_URL: postgresql://agent:agent@db:5432/agent_runs
      API_BASE_URL: http://proxy:8000
      MERCURY_BEARER_TOKEN: ${MERCURY_BEARER_TOKEN:-}
      MERCURY_CONFIG_PATH: /config/mercury.yaml
    volumes:
      - ./config/mercury.sample.yaml:/config/mercury.yaml:ro
    depends_on:
      db:
        condition: service_healthy
```

The `profiles` key keeps it out of `docker compose up`; it is invoked explicitly (`docker compose run --rm scheduler`) since a Job is one-shot, not a long-running service. It mounts the sample config directly rather than requiring the base64 secret path (`MERCURY_CONFIG_B64`), which exists for the deployed image, not local compose.

- [ ] **Step 3: Document the three new env vars**

Add to `.env.example`, after the `ANTHROPIC_API_KEY=` line:

```
# The scheduler (Mercury step 2a). Empty MERCURY_BEARER_TOKEN means the
# scheduler cannot create runs, and POST /runs refuses every non-public
# task type. API_BASE_URL is where the scheduler posts to; MERCURY_CONFIG_PATH
# is where it expects mercury.yaml to be mounted.
MERCURY_BEARER_TOKEN=
API_BASE_URL=http://localhost:8000
MERCURY_CONFIG_PATH=/config/mercury.yaml
```

- [ ] **Step 4: Verify manually against compose**

```bash
# The api reads the token when `up` starts it, so it has to be exported first.
export MERCURY_BEARER_TOKEN=local-test-token
docker compose up --build -d --wait
docker compose run --rm scheduler
```

Expected: log lines including `scheduled run <uuid> created with no client` for the one site in `config/mercury.sample.yaml`'s `portfolio.sites` (`https://example.com` by default — replace with something reachable, or a path the running compose api itself serves, such as `http://api:8000/health`, if a network call to a real external site is undesirable during this check). Confirm the run shows up:

```bash
curl -s "http://localhost:8000/runs?limit=5" | python3 -m json.tool
```

Expected: one `site_check` row, `status` either `succeeded` or `failed` depending on what was actually reachable, never left `pending`.

```bash
docker compose down
```

- [ ] **Step 5: Commit**

```bash
git add docker/entrypoint.sh docker-compose.yml .env.example
git commit -m "Wire ROLE=scheduler into the image and compose

The entrypoint decodes a base64 mercury.yaml secret if one is mounted,
before dispatching on ROLE, and gains a third case alongside api and
worker. docker-compose.yml gets a scheduler service behind a profile, run
by hand rather than by `up`, since a Job is one-shot. Verified against the
compose stack: a run posted with no client attached, its status left
succeeded or failed rather than pending."
```

---

## Task 7: Bicep: the Container Apps Job

**Files:**
- Modify: `infra/main.bicep` (new params, new optional secrets, the scheduler Job resource, `MERCURY_BEARER_TOKEN` wired into `api`)
- Modify: `infra/deploy.sh` (pass the two new params through)
- Modify: `.github/workflows/deploy.yml` (pass the two new secrets/vars through, guarded so a deploy with neither still succeeds)

**Interfaces:** None new — this is infrastructure only, no Python.

- [ ] **Step 1: Add the two new params**

In `infra/main.bicep`, after the `voyageApiKey` param:

```bicep
@description('Bearer token non-public callers (the scheduler, later Telegram) use against POST /runs. Empty leaves every non-public type refusing all callers.')
@secure()
param mercuryBearerToken string = ''

@description('mercury.yaml, base64 encoded, mounted for the scheduler. Empty means no config, and the scheduler refuses to start.')
@secure()
param mercuryConfigB64 string = ''
```

- [ ] **Step 2: Add them to `optionalSecrets`**

```bicep
var optionalSecrets = concat(
  empty(modelApiKey)
    ? []
    : [
        {
          name: 'model-api-key'
          value: modelApiKey
        }
      ],
  empty(voyageApiKey)
    ? []
    : [
        {
          name: 'voyage-api-key'
          value: voyageApiKey
        }
      ],
  empty(mercuryBearerToken)
    ? []
    : [
        {
          name: 'mercury-bearer-token'
          value: mercuryBearerToken
        }
      ],
  empty(mercuryConfigB64)
    ? []
    : [
        {
          name: 'mercury-config'
          value: mercuryConfigB64
        }
      ]
)
```

(Replaces the existing `optionalSecrets` variable; `sharedSecrets` already concatenates it, so both `api` and `worker` pick up whichever of the four optional secrets are non-empty automatically, with no further change needed there.)

- [ ] **Step 3: Give the `api` app the bearer token env var**

In the `api` resource's `env`, after the `OTEL_SERVICE_NAME` entry:

```bicep
          env: concat(
            sharedEnvironment,
            [
              {
                name: 'ROLE'
                value: 'api'
              }
              {
                name: 'OTEL_SERVICE_NAME'
                value: '${name}-api'
              }
            ],
            empty(mercuryBearerToken)
              ? []
              : [
                  {
                    name: 'MERCURY_BEARER_TOKEN'
                    secretRef: 'mercury-bearer-token'
                  }
                ]
          )
```

- [ ] **Step 4: Add the scheduler Job resource**

After the `worker` resource, before the `output` lines:

```bicep
var schedulerEnvironment = concat(
  empty(mercuryBearerToken)
    ? []
    : [
        {
          name: 'MERCURY_BEARER_TOKEN'
          secretRef: 'mercury-bearer-token'
        }
      ],
  empty(mercuryConfigB64)
    ? []
    : [
        {
          name: 'MERCURY_CONFIG_B64'
          secretRef: 'mercury-config'
        }
      ]
)

resource scheduler 'Microsoft.App/jobs@2024-03-01' = {
  name: '${name}-scheduler'
  location: location
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      // Hourly matches config/mercury.sample.yaml's schedule.site_uptime.
      // A per-entry cron read from the mounted config, so a schedule change
      // does not need a redeploy, is later work once more than one
      // schedule entry has an executor.
      scheduleTriggerConfig: {
        cronExpression: '0 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 300
      replicaRetryLimit: 0
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'scheduler'
          image: image
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat(
            sharedEnvironment,
            [
              {
                name: 'ROLE'
                value: 'scheduler'
              }
              {
                name: 'OTEL_SERVICE_NAME'
                value: '${name}-scheduler'
              }
              {
                name: 'API_BASE_URL'
                value: 'https://${api.properties.configuration.ingress.fqdn}'
              }
            ],
            schedulerEnvironment
          )
        }
      ]
    }
  }
}
```

- [ ] **Step 5: Add the output**

After `output workerName string = worker.name`:

```bicep
output schedulerName string = scheduler.name
```

- [ ] **Step 6: Validate the Bicep compiles**

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null
```

Expected: no output on stderr, exit code 0. This catches syntax and schema errors; it does not validate against a live subscription (no Azure credentials in this environment), so treat this as necessary, not sufficient, and expect the actual `deploy` job in CI to be the real proof once secrets exist.

- [ ] **Step 7: Update `infra/deploy.sh`**

Add two lines to the `az deployment group create --parameters` block, after `voyageApiKey="${VOYAGE_API_KEY:-}"`:

```bash
      mercuryBearerToken="${MERCURY_BEARER_TOKEN:-}" \
      mercuryConfigB64="${MERCURY_CONFIG_B64:-}" \
```

- [ ] **Step 8: Update `.github/workflows/deploy.yml`**

Add two lines to the `Deploy the Bicep template` step's `env`, after `VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}`:

```yaml
          MERCURY_BEARER_TOKEN: ${{ secrets.MERCURY_BEARER_TOKEN }}
          MERCURY_CONFIG_B64: ${{ secrets.MERCURY_CONFIG_B64 }}
```

Both read from secrets that do not exist on this repo yet (confirmed via `gh secret list` earlier in this session); with them unset, `${{ secrets.X }}` evaluates to an empty string in Actions, which `infra/deploy.sh` passes through as `''`, which the Bicep's `empty()` checks handle the same way local compose does with no `MERCURY_BEARER_TOKEN` set: the scheduler Job is not deployed at all (`deployScheduler` in the Bicep needs both values), so it cannot start and fail every hour, and `api`/`worker` are unaffected. As built, the Job also gets its own secret list rather than `sharedSecrets`, and the worker's scale rule excludes `site_check`. This is the deferred half of 2a's exit condition: the code path is real and will start working the moment the two secrets are added, with no further change.

- [ ] **Step 9: Commit**

```bash
git add infra/main.bicep infra/deploy.sh .github/workflows/deploy.yml
git commit -m "Add the scheduler Container Apps Job to the Bicep

A third resource alongside api and worker, on an hourly Schedule trigger
matching mercury.sample.yaml's site_uptime, on the same free grant.
mercuryBearerToken and mercuryConfigB64 are new optional secure params,
following the existing empty-string-means-absent pattern the model and
voyage keys already use. MERCURY_BEARER_TOKEN and MERCURY_CONFIG_B64 do not
exist as secrets on this repo yet, so the scheduler will deploy but refuse
to start until they are added; api and worker are unaffected either way.
Verified locally with `az bicep build`, which is necessary but not
sufficient without a real subscription to deploy against."
```

---

## What this plan defers

- **The live-deploy proof of 2a's exit condition** (the `scheduled run <id> created with no client` log line from the real deployment, and turning README claim one green) waits on `MERCURY_BEARER_TOKEN` and `MERCURY_CONFIG_B64` existing as secrets on this repo and the private `mercury-config` repo, both currently unset. Task 6 and Task 7 prove the same behavior locally against compose instead.
- **Reading a per-entry cron schedule from `mercury.yaml`** rather than relying on the Job's own hourly Bicep trigger. Deferred until a second schedule type (`ci_watch`, weekly `lighthouse`, and so on) actually needs a different cadence; adding it now would be code with no caller.
- **The claim endpoints** (`POST /checks/claim`, `/heartbeat`, `/result`) are step 2b, for the self hosted Lighthouse worker. Being terminal by the time `run_due_checks` returns does not protect a `site_check` run, because a poller can claim it between creation and close. The agent loop worker's claim query excludes `site_check` for that reason, and 2b's claim logic will need the same care. 2b's checks worker will also set `claimed_by` on `site_check` runs, so the daily limit count in `app/worker.py` will need to count public types only at that point.
- **Resuming a suspended schedule from Telegram** (inline button, `/status`, digest visibility) is step 3. `app/schedule_state.resume` exists and is tested; nothing calls it yet outside the test suite.

## Self-review

- **Spec coverage:** every clause of the 2a bullet in `docs/build-brief-mercury.md` maps to a task — the Bicep Job and cron trigger (Task 7), `ROLE=scheduler` (Task 6), `site_check` type with no model call (Task 4, and `app/tasks.py` already has the registry entry from step 1), posting with the bearer token (Task 2, Task 5), schedule read from the mounted config (Task 1, Task 6), the scheduler's own `OTEL_SERVICE_NAME` (Task 7) and explicit flush before exit (Task 5), and suspension on the third consecutive failure with a resume path (Task 3). The exit condition's test half (suspends on the third failure, resumes) is Task 3's test suite; the log-line half is Task 5's test suite plus Task 6's manual compose verification; the live-deploy half is explicitly deferred above.
- **Placeholder scan:** no task contains "TBD," "add error handling," or an undefined reference; every code block is complete and copy-pasteable.
- **Type consistency:** `run_due_checks(conn, sites, api_base_url, bearer_token) -> list[str]` in Task 5 matches its three call sites in Task 5's own tests. `SiteCheckResult` (Task 4) and `MercuryConfig` (Task 1) are each defined once and consumed with the same field names everywhere they appear (Task 5's `run_due_checks` reads `result.status_code`, `result.latency_ms`, `result.passed`, `result.error`, all of which Task 4 defines). `is_suspended`/`record_failure`/`record_success`/`resume` (Task 3) all take `(conn, name)` consistently across their definition and every call site in Tasks 3 and 5.
