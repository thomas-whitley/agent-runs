// The checks worker's entry point. It polls the API for a Lighthouse check,
// runs one at a time, and sleeps POLL_SECONDS whenever there is nothing to
// do or the API cannot be reached. `--once` polls a single time and exits.
//
// Environment: API_BASE_URL and MERCURY_BEARER_TOKEN are required,
// WORKER_ID defaults to the hostname, POLL_SECONDS to 60.

import { hostname } from "node:os";
import { setTimeout as sleep } from "node:timers/promises";

import { ChecksApi } from "./api.js";
import { runLighthouse } from "./lighthouse.js";
import { createLogger } from "./log.js";
import { pollOnce } from "./worker.js";

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set`);
  }
  return value;
}

const workerId = process.env.WORKER_ID || hostname();
const pollMs = Number(process.env.POLL_SECONDS ?? "60") * 1000;
const once = process.argv.includes("--once");
const log = createLogger({ workerId });

const api = new ChecksApi({
  baseUrl: required("API_BASE_URL").replace(/\/$/, ""),
  token: required("MERCURY_BEARER_TOKEN"),
  workerId,
});
const deps = { api, runLighthouse, log, heartbeatMs: 30_000 };

// A signal ends an idle sleep at once, and lets a running check finish.
const stop = new AbortController();
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    stop.abort();
    log.info(`${signal} received, stopping after the current check`);
  });
}

log.info(`checks worker started, polling every ${pollMs / 1000}s`);
while (!stop.signal.aborted) {
  const outcome = await pollOnce(deps);
  if (once) {
    break;
  }
  if (outcome === "idle" || outcome === "error") {
    await sleep(pollMs, undefined, { signal: stop.signal }).catch(() => {});
  }
}
log.info("checks worker stopped");
