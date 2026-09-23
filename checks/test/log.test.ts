import { describe, expect, it } from "vitest";

import { createLogger } from "../src/log.js";

function logger(keep?: number) {
  const lines: string[] = [];
  const log = createLogger({ workerId: "laptop-1", write: (line) => lines.push(line), keep });
  return { log, lines };
}

describe("createLogger", () => {
  it("writes one JSON object per line with the fields the Python side writes", () => {
    const { log, lines } = logger();

    log.info("claimed check abc", { run_id: "abc" });

    const entry = JSON.parse(lines[0] ?? "");
    expect(entry).toMatchObject({
      level: "INFO",
      logger: "agent_runs.checks",
      message: "claimed check abc",
      run_id: "abc",
      worker_id: "laptop-1",
      executor: "self_hosted",
    });
    expect(new Date(entry.timestamp).toISOString()).toBe(entry.timestamp);
  });

  it("names levels the way Python's logging does", () => {
    const { log, lines } = logger();

    log.warn("a");
    log.error("b");

    expect(lines.map((line) => JSON.parse(line).level)).toEqual(["WARNING", "ERROR"]);
  });

  it("keeps only the last lines it was told to, for a failure report", () => {
    const { log } = logger(3);

    for (let i = 1; i <= 5; i++) {
      log.info(`line ${i}`);
    }

    expect(log.tail().map((line) => JSON.parse(line).message)).toEqual([
      "line 3",
      "line 4",
      "line 5",
    ]);
  });
});
