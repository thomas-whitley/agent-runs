import { describe, expect, it } from "vitest";

import { summarize } from "../src/summary.js";

// The parts of a Lighthouse result (LHR) the summary reads. A real one is
// several hundred KB; the summary keeps about 1 KB of it.
function lhr(overrides: Record<string, unknown> = {}) {
  return {
    lighthouseVersion: "13.5.0",
    finalDisplayedUrl: "https://example.com/",
    categories: {
      performance: { score: 0.91 },
      accessibility: { score: 1 },
      "best-practices": { score: 0.78 },
      seo: { score: 0.9 },
      "agentic-browsing": { score: null },
    },
    audits: {
      "largest-contentful-paint": { score: 0.95, scoreDisplayMode: "numeric", numericValue: 1834.2 },
      "total-blocking-time": { score: 1, scoreDisplayMode: "numeric", numericValue: 12.5 },
      "image-alt": { score: 0, scoreDisplayMode: "binary" },
      "uses-http2": { score: 0.5, scoreDisplayMode: "metricSavings" },
      "is-on-https": { score: 1, scoreDisplayMode: "binary" },
      "font-size": { score: 0.89, scoreDisplayMode: "binary" },
      "network-requests": { score: null, scoreDisplayMode: "informative" },
      "manual-check": { score: 0, scoreDisplayMode: "manual" },
      "not-here": { score: 0, scoreDisplayMode: "notApplicable" },
    },
    ...overrides,
  };
}

describe("summarize", () => {
  it("keeps the five category scores", () => {
    expect(summarize(lhr()).scores).toEqual({
      performance: 0.91,
      accessibility: 1,
      "best-practices": 0.78,
      seo: 0.9,
      "agentic-browsing": null,
    });
  });

  it("keeps largest contentful paint and total blocking time in whole milliseconds", () => {
    const summary = summarize(lhr());

    expect(summary.lcp_ms).toBe(1834);
    expect(summary.tbt_ms).toBe(13);
  });

  it("lists the audits that scored under Lighthouse's pass mark of 0.9, sorted", () => {
    expect(summarize(lhr()).failed_audits).toEqual(["font-size", "image-alt", "uses-http2"]);
  });

  it("records which Lighthouse produced it and the page it ended on", () => {
    const summary = summarize(lhr());

    expect(summary.lighthouse_version).toBe("13.5.0");
    expect(summary.final_url).toBe("https://example.com/");
  });

  it("reports a metric Lighthouse could not measure as null rather than failing", () => {
    const summary = summarize(lhr({ audits: {} }));

    expect(summary.lcp_ms).toBeNull();
    expect(summary.tbt_ms).toBeNull();
    expect(summary.failed_audits).toEqual([]);
  });

  it("stays near a kilobyte even when many audits fail", () => {
    const audits: Record<string, unknown> = {};
    for (let i = 0; i < 40; i++) {
      audits[`some-failing-audit-${i}`] = { score: 0, scoreDisplayMode: "binary" };
    }

    expect(JSON.stringify(summarize(lhr({ audits }))).length).toBeLessThan(2048);
  });
});
