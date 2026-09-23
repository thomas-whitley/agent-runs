import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ClaimedCheck, type Closed, type Held } from "../src/api.js";
import { createLogger } from "../src/log.js";
import type { LighthouseResult } from "../src/summary.js";
import { pollOnce, runOnce, type WorkerDeps } from "../src/worker.js";

const CHECK: ClaimedCheck = {
  id: "0b6f2c1e-8d1a-4f3e-9a47-2f1c6d5e4b3a",
  url: "https://example.com",
  kind: "lighthouse",
  lease_seconds: 120,
};

const REPORT: LighthouseResult = {
  lighthouseVersion: "13.5.0",
  finalDisplayedUrl: "https://example.com/",
  categories: { performance: { score: 0.91 } },
  audits: { "largest-contentful-paint": { score: 1, numericValue: 1200 } },
};

// A fake of the API client with scripted answers, recording every call.
function fakeApi(options: { claim?: ClaimedCheck | null; heartbeat?: Held; result?: Closed } = {}) {
  const calls: { heartbeats: string[]; results: { id: string; result: Record<string, unknown> }[] } =
    { heartbeats: [], results: [] };
  const api = {
    claim: vi.fn(async () => (options.claim === undefined ? CHECK : options.claim)),
    heartbeat: vi.fn(async (id: string) => {
      calls.heartbeats.push(id);
      return options.heartbeat ?? "held";
    }),
    postResult: vi.fn(async (id: string, result: Record<string, unknown>) => {
      calls.results.push({ id, result });
      return options.result ?? "closed";
    }),
  };
  return { api, calls };
}

function deps(api: WorkerDeps["api"], runLighthouse: WorkerDeps["runLighthouse"]) {
  const lines: string[] = [];
  const log = createLogger({ workerId: "laptop-1", write: (line) => lines.push(line) });
  const workerDeps: WorkerDeps = { api, runLighthouse, log, heartbeatMs: 30_000 };
  return { workerDeps, lines };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("runOnce", () => {
  it("is idle when there is nothing to claim", async () => {
    const { api, calls } = fakeApi({ claim: null });
    const { workerDeps } = deps(api, async () => REPORT);

    expect(await runOnce(workerDeps)).toBe("idle");
    expect(calls.results).toEqual([]);
  });

  it("claims lighthouse checks only", async () => {
    const { api } = fakeApi({ claim: null });
    const { workerDeps } = deps(api, async () => REPORT);

    await runOnce(workerDeps);

    expect(api.claim).toHaveBeenCalledWith(["lighthouse"]);
  });

  it("runs Lighthouse on the claimed URL and posts the summary, with no log tail", async () => {
    const { api, calls } = fakeApi();
    const runLighthouse = vi.fn(async () => REPORT);
    const { workerDeps } = deps(api, runLighthouse);

    expect(await runOnce(workerDeps)).toBe("closed");

    expect(runLighthouse).toHaveBeenCalledWith("https://example.com");
    expect(calls.results).toHaveLength(1);
    expect(calls.results[0]?.id).toBe(CHECK.id);
    expect(calls.results[0]?.result).toMatchObject({ scores: { performance: 0.91 }, lcp_ms: 1200 });
    expect(calls.results[0]?.result).not.toHaveProperty("log_tail");
  });

  it("posts the error and the last fifty log lines when Lighthouse fails", async () => {
    const { api, calls } = fakeApi();
    const { workerDeps } = deps(api, async () => {
      throw new Error("Chrome could not be started");
    });
    for (let i = 0; i < 60; i++) {
      workerDeps.log.info(`earlier line ${i}`);
    }

    expect(await runOnce(workerDeps)).toBe("failed");

    const result = calls.results[0]?.result as { error: string; log_tail: string[] };
    expect(result.error).toBe("Chrome could not be started");
    expect(result.log_tail).toHaveLength(50);
    expect(JSON.parse(result.log_tail.at(-1) ?? "").message).toContain("Chrome could not be started");
  });

  it("heartbeats every 30 seconds while Lighthouse runs, and stops after", async () => {
    vi.useFakeTimers();
    const { api, calls } = fakeApi();
    let finish: (report: LighthouseResult) => void = () => {};
    const { workerDeps } = deps(api, () => new Promise((resolve) => (finish = resolve)));

    const outcome = runOnce(workerDeps);
    await vi.advanceTimersByTimeAsync(65_000);
    expect(calls.heartbeats).toEqual([CHECK.id, CHECK.id]);

    finish(REPORT);
    expect(await outcome).toBe("closed");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls.heartbeats).toHaveLength(2);
  });

  it("drops the result when a heartbeat says the check was lost", async () => {
    vi.useFakeTimers();
    const { api, calls } = fakeApi({ heartbeat: "lost" });
    let finish: (report: LighthouseResult) => void = () => {};
    const { workerDeps } = deps(api, () => new Promise((resolve) => (finish = resolve)));

    const outcome = runOnce(workerDeps);
    await vi.advanceTimersByTimeAsync(31_000);
    finish(REPORT);

    expect(await outcome).toBe("lost");
    expect(calls.results).toEqual([]);
  });

  it("keeps running when one heartbeat fails on the network", async () => {
    vi.useFakeTimers();
    const { api } = fakeApi();
    api.heartbeat.mockRejectedValueOnce(new TypeError("fetch failed"));
    let finish: (report: LighthouseResult) => void = () => {};
    const { workerDeps } = deps(api, () => new Promise((resolve) => (finish = resolve)));

    const outcome = runOnce(workerDeps);
    await vi.advanceTimersByTimeAsync(61_000);
    finish(REPORT);

    expect(await outcome).toBe("closed");
    expect(api.heartbeat).toHaveBeenCalledTimes(2);
  });

  it("reports lost when the result arrives after another worker took the check", async () => {
    const { api } = fakeApi({ result: "lost" });
    const { workerDeps } = deps(api, async () => REPORT);

    expect(await runOnce(workerDeps)).toBe("lost");
  });
});

describe("pollOnce", () => {
  it("logs an API failure and carries on rather than throwing", async () => {
    const { api } = fakeApi();
    api.claim.mockRejectedValueOnce(new ApiError("/checks/claim", 503));
    const { workerDeps, lines } = deps(api, async () => REPORT);

    expect(await pollOnce(workerDeps)).toBe("error");
    expect(lines.some((line) => line.includes("/checks/claim answered 503"))).toBe(true);
  });
});
