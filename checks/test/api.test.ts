import { describe, expect, it } from "vitest";

import { ApiError, ChecksApi } from "../src/api.js";
import { fakeFetch } from "./fake-fetch.js";

const CHECK = {
  id: "0b6f2c1e-8d1a-4f3e-9a47-2f1c6d5e4b3a",
  url: "https://example.com",
  kind: "lighthouse",
  lease_seconds: 120,
};

function api(replies: Parameters<typeof fakeFetch>[0]) {
  const fake = fakeFetch(replies);
  const client = new ChecksApi({
    baseUrl: "https://api.test",
    token: "the-token",
    workerId: "laptop-1",
    fetchImpl: fake.fetchImpl,
  });
  return { client, sent: fake.sent };
}

describe("claim", () => {
  it("posts the worker id and its kinds with the bearer token, and returns the check", async () => {
    const { client, sent } = api([{ status: 200, body: CHECK }]);

    expect(await client.claim(["lighthouse"])).toEqual(CHECK);
    expect(sent[0]).toMatchObject({
      url: "https://api.test/checks/claim",
      method: "POST",
      body: { worker_id: "laptop-1", kinds: ["lighthouse"] },
    });
    expect(sent[0]?.headers["authorization"]).toBe("Bearer the-token");
  });

  it("returns null on a 204, when there is nothing to claim", async () => {
    const { client } = api([{ status: 204 }]);

    expect(await client.claim(["lighthouse"])).toBeNull();
  });

  it("throws on anything else, naming the status", async () => {
    const { client } = api([{ status: 401, body: { detail: "missing or invalid bearer token" } }]);

    await expect(client.claim(["lighthouse"])).rejects.toThrow(ApiError);
    await expect(api([{ status: 503 }]).client.claim(["lighthouse"])).rejects.toThrow("503");
  });
});

describe("heartbeat", () => {
  it("reports the check still held on a 200", async () => {
    const { client, sent } = api([{ status: 200, body: { lease_seconds: 120 } }]);

    expect(await client.heartbeat(CHECK.id)).toBe("held");
    expect(sent[0]).toMatchObject({
      url: `https://api.test/checks/${CHECK.id}/heartbeat`,
      body: { worker_id: "laptop-1" },
    });
  });

  it("reports the check lost on a 409", async () => {
    const { client } = api([{ status: 409 }]);

    expect(await client.heartbeat(CHECK.id)).toBe("lost");
  });
});

describe("postResult", () => {
  it("posts the result and reports the check closed on a 200", async () => {
    const { client, sent } = api([{ status: 200, body: { status: "succeeded" } }]);

    expect(await client.postResult(CHECK.id, { lcp_ms: 1834 })).toBe("closed");
    expect(sent[0]).toMatchObject({
      url: `https://api.test/checks/${CHECK.id}/result`,
      body: { worker_id: "laptop-1", result: { lcp_ms: 1834 } },
    });
  });

  it("reports the check lost on a 409", async () => {
    const { client } = api([{ status: 409 }]);

    expect(await client.postResult(CHECK.id, {})).toBe("lost");
  });
});
