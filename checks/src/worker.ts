// One cycle of the self hosted checks worker: claim a Lighthouse check, run
// it while heartbeating, and post the summary. Broken link crawls are
// claimed here too once the crawl exists (build brief step 2d).

import type { ClaimedCheck, Closed, Held } from "./api.js";
import type { Logger } from "./log.js";
import { type LighthouseResult, summarize } from "./summary.js";

export const KINDS = ["lighthouse"];

export interface WorkerApi {
  claim(kinds: string[]): Promise<ClaimedCheck | null>;
  heartbeat(id: string): Promise<Held>;
  postResult(id: string, result: Record<string, unknown>): Promise<Closed>;
}

export interface WorkerDeps {
  api: WorkerApi;
  runLighthouse: (url: string) => Promise<LighthouseResult>;
  log: Logger;
  // 30 seconds against a two minute lease, per docs/mercury.md.
  heartbeatMs: number;
}

export type Outcome = "idle" | "closed" | "failed" | "lost";

function reason(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function runOnce(deps: WorkerDeps): Promise<Outcome> {
  const { api, log } = deps;
  const check = await api.claim(KINDS);
  if (check === null) {
    return "idle";
  }
  const fields = { run_id: check.id };
  log.info(`claimed ${check.kind} check ${check.id} for ${check.url}`, fields);

  let lost = false;
  const beat = setInterval(() => {
    api.heartbeat(check.id).then(
      (held) => {
        if (held === "lost" && !lost) {
          lost = true;
          log.warn(`lost check ${check.id} to another worker`, fields);
        }
      },
      // One missed heartbeat is survivable; the lease allows three more.
      (error: unknown) => log.warn(`heartbeat failed: ${reason(error)}`, fields),
    );
  }, deps.heartbeatMs);

  let result: Record<string, unknown>;
  let failed = false;
  try {
    const summary = summarize(await deps.runLighthouse(check.url));
    result = { ...summary };
  } catch (error) {
    failed = true;
    log.error(`lighthouse failed for check ${check.id}: ${reason(error)}`, fields);
    result = { error: reason(error), log_tail: log.tail() };
  } finally {
    clearInterval(beat);
  }

  if (lost) {
    log.warn(`dropped the result of check ${check.id}, which another worker holds`, fields);
    return "lost";
  }
  if ((await api.postResult(check.id, result)) === "lost") {
    log.warn(`check ${check.id} was closed or taken over before its result arrived`, fields);
    return "lost";
  }
  log.info(`check ${check.id} closed`, fields);
  return failed ? "failed" : "closed";
}

// The API being unreachable or refusing the token is logged and retried on
// the next poll, rather than ending the process.
export async function pollOnce(deps: WorkerDeps): Promise<Outcome | "error"> {
  try {
    return await runOnce(deps);
  } catch (error) {
    deps.log.error(`poll failed: ${reason(error)}`);
    return "error";
  }
}
