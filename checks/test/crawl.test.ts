import { describe, expect, it } from "vitest";

import { crawl, parseRobots, robotsAllows } from "../src/crawl.js";
import { fakeSite, links } from "./fake-site.js";

const O = "https://site.test";

describe("crawl", () => {
  it("follows same origin links and reports a 404 with the page it was found on", async () => {
    const site = fakeSite({
      [`${O}/`]: { html: links("/about", "/gone") },
      [`${O}/about`]: { html: links("/") },
    });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(result.pages_checked).toBe(3);
    expect(result.broken).toEqual([{ url: `${O}/gone`, status: 404, found_on: `${O}/` }]);
    expect(result.broken_count).toBe(1);
  });

  it("never requests another origin", async () => {
    const site = fakeSite({
      [`${O}/`]: { html: links("https://elsewhere.test/", "http://site.test/", "//cdn.test/x.js") },
    });

    await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(site.requested.filter((url) => !url.startsWith(O))).toEqual([]);
  });

  it("reports 5xx, timeouts and refused connections, and nothing that answered below 400", async () => {
    const site = fakeSite({
      [`${O}/`]: { html: links("/ok", "/moved", "/boom", "/slow", "/refused") },
      [`${O}/ok`]: { html: "" },
      [`${O}/moved`]: { status: 304 },
      [`${O}/boom`]: { status: 503 },
      [`${O}/slow`]: { hang: true },
      [`${O}/refused`]: { refuse: true },
    });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl, timeoutMs: 50 });

    expect(result.broken).toEqual([
      { url: `${O}/boom`, status: 503, found_on: `${O}/` },
      { url: `${O}/slow`, status: "timeout", found_on: `${O}/` },
      { url: `${O}/refused`, status: "error", found_on: `${O}/` },
    ]);
  });

  it("requests pages to depth three and no deeper", async () => {
    const site = fakeSite({
      [`${O}/`]: { html: links("/1") },
      [`${O}/1`]: { html: links("/2") },
      [`${O}/2`]: { html: links("/3") },
      [`${O}/3`]: { html: links("/4") },
    });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(site.requested).not.toContain(`${O}/4`);
    expect(site.requested).toContain(`${O}/3`);
    expect(result.pages_checked).toBe(4);
  });

  it("stops at the page limit and says so", async () => {
    const many = Array.from({ length: 300 }, (_, i) => `/p${i}`);
    const pages = Object.fromEntries(many.map((path) => [`${O}${path}`, { html: "" }]));
    const site = fakeSite({ [`${O}/`]: { html: links(...many) }, ...pages });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(result.pages_checked).toBe(200);
    expect(result.page_limit_reached).toBe(true);
    expect(site.requested.filter((url) => url !== `${O}/robots.txt`)).toHaveLength(200);
  });

  it("skips paths robots.txt disallows for every agent", async () => {
    const site = fakeSite({
      [`${O}/robots.txt`]: {
        contentType: "text/plain",
        html: "User-agent: googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n",
      },
      [`${O}/`]: { html: links("/private/a", "/public") },
      [`${O}/public`]: { html: "" },
    });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(site.requested).not.toContain(`${O}/private/a`);
    expect(site.requested).toContain(`${O}/public`);
    expect(result.robots_skipped).toBe(1);
  });

  it("crawls everything when there is no robots.txt", async () => {
    const site = fakeSite({ [`${O}/`]: { html: links("/a") }, [`${O}/a`]: { html: "" } });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(site.requested).toContain(`${O}/a`);
    expect(result.robots_skipped).toBe(0);
  });

  it("resolves relative links, drops fragments, requests each page once and ignores other schemes", async () => {
    const site = fakeSite({
      [`${O}/docs/`]: {
        html: links("guide", "./guide#top", "/docs/guide", "mailto:a@b.test", "javascript:void(0)", "tel:1"),
      },
      [`${O}/docs/guide`]: { html: "" },
    });

    await crawl(`${O}/docs/`, { fetchImpl: site.fetchImpl });

    expect(site.requested.filter((url) => url === `${O}/docs/guide`)).toHaveLength(1);
    expect(site.requested.every((url) => url.startsWith(`${O}/`))).toBe(true);
  });

  it("reads links only out of HTML", async () => {
    const site = fakeSite({
      [`${O}/`]: { html: links("/data.json") },
      [`${O}/data.json`]: { contentType: "application/json", html: '{"x": "<a href=\\"/hidden\\">"}' },
    });

    await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(site.requested).not.toContain(`${O}/hidden`);
  });

  it("reports a broken start page with no page it was found on", async () => {
    const site = fakeSite({ [`${O}/`]: { status: 500 } });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(result.broken).toEqual([{ url: `${O}/`, status: 500, found_on: null }]);
  });

  it("keeps the first fifty broken links and counts the rest, so the result stays small", async () => {
    const dead = Array.from({ length: 150 }, (_, i) => `/a-rather-long-path-name-for-a-dead-page-${i}`);
    const site = fakeSite({ [`${O}/`]: { html: links(...dead) } });

    const result = await crawl(`${O}/`, { fetchImpl: site.fetchImpl });

    expect(result.broken).toHaveLength(50);
    expect(result.broken_count).toBe(150);
    expect(JSON.stringify(result).length).toBeLessThan(16_384);
  });
});

describe("parseRobots", () => {
  function allowed(robots: string, path: string): boolean {
    return robotsAllows(parseRobots(robots), path);
  }

  it("prefers the group naming this crawler over the * group", () => {
    const robots = "User-agent: *\nDisallow: /\n\nUser-agent: mercury-checks\nDisallow: /tmp\n";

    expect(allowed(robots, "/page")).toBe(true);
    expect(allowed(robots, "/tmp/x")).toBe(false);
  });

  it("lets a longer Allow win over a shorter Disallow", () => {
    const robots = "User-agent: *\nDisallow: /docs\nAllow: /docs/public\n";

    expect(allowed(robots, "/docs/secret")).toBe(false);
    expect(allowed(robots, "/docs/public/a")).toBe(true);
  });

  it("reads * and $ in a rule", () => {
    const robots = "User-agent: *\nDisallow: /*.pdf$\n";

    expect(allowed(robots, "/files/a.pdf")).toBe(false);
    expect(allowed(robots, "/files/a.pdf?x=1")).toBe(true);
  });
});
