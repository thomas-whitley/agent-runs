# Fix brief after the first code review, 2026-09-21

Paste into the build session. Work top down; each fix starts with a failing test that reproduces it, then the change, then the README updated only if a claim's wording changes. Delete this file in the first fix commit.

## 1. Reconnecting after `done` hangs the stream (app/stream.py, about lines 50 to 68)

The generator returns only when it finds a `done` row newer than `Last-Event-ID`. A client whose last seen id is the `done` event reconnects, the `id > %s` query returns nothing forever, and the loop sleeps 50 ms and polls Postgres indefinitely, sending keepalives. On Container Apps that holds the replica above zero and breaks scale to zero.

Test first: in `tests/test_resume.py`, consume a full stream to `done`, reconnect with `Last-Event-ID` equal to the `done` event's id, assert the response closes (either empty or re-emitting `done`) within a second.

Fix: on an empty poll, check whether the run's latest event is `done` with id at or below `after_id` (or `runs.finished_at IS NOT NULL`) and return. Re-emitting `done` is acceptable and matches `docs/design.md`.

## 2. Worker takeover is not fenced (app/worker.py:81, app/runs.py:37 to 47, app/loop.py:205 to 208, app/model.py:57 to 63 and 92 to 104)

A worker whose lease expires during a slow model call keeps writing. `heartbeat()` returns false when ownership is lost and the return value is discarded. `_INSERT_STEP` and `_INSERT_EVENT` do not check `claimed_by`. The final `UPDATE runs SET status ...` is unconditional. Model calls have no timeout while `LEASE_SECONDS` is 60.

Test first: two connections, one holding a run whose lease has been forced to expire, the other taking it over; the first attempts a step write and a final status update; assert both are rejected and the second worker's status wins.

Fix: pass `timeout=` to both model clients at well under the lease; add `AND claimed_by = %s` to the step insert, the event insert and the final status update; make the loop stop without writing when `heartbeat()` returns false. Refresh the heartbeat before each model call as well as between steps.

## 3. The sandbox sentence overstates what it does (app/sandbox.py:17 to 36, README and docs/design.md)

"No network inside the worker container" is not true; the container calls the model and Postgres. The subprocess isolation is a Python level patch of `socket` that `import _socket` bypasses. The environment scrub (env replaced, no keys reach generated code) is correct and stays.

Fix: reword to what it does ("the verification subprocess runs with a scrubbed environment, a timeout, and Python's socket module disabled; this is a demo guard, not isolation"). Cheap hardening if wanted: also block `_socket`, or run the subprocess under `unshare -n` when available.

## 4. Trace id claim has no code (docs/design.md, app/runs.py:88)

"The trace id rides on every event payload" is not implemented anywhere. Either add the current span's trace id to the payload in `record_step` with a test that reads it back, or delete the sentence.

## 5. Ingress request cap (infra/main.bicep:111 to 118, docs/design.md)

Container Apps consumption ingress cuts any request at 240 seconds; keepalives do not extend it and raising it needs paid ingress. A ten attempt run can exceed 240 seconds. State this in the design doc and README, and make the demo page reconnect on close, which works once fix 1 is in.

## Also

- Run `uv run ruff check .` before the next push; the reviewer could not run it on the other machine.
- `MAX_RUNS_PER_DAY` is check then act; it is safe only because the worker runs at one replica. One code comment saying so.
- Nothing sensitive was found in the tree.

## Leave alone

The `id > Last-Event-ID` query, the `(run_id, seq)` conflict handling, the sandbox environment scrub, the nginx SSE settings, the corpus curation, and the real socket test harness are all correct and were checked.
