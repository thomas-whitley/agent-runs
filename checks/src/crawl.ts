// The broken link check: a same origin crawl from the checked URL, depth
// three, two hundred pages, a ten second timeout per request, robots.txt
// respected. Only 4xx, 5xx, timeouts and requests that could not connect
// are reported, per docs/mercury.md. The result is posted as it is, so it
// is kept well under the API's 16 KB limit.

export const USER_AGENT = "mercury-checks";

export interface CrawlOptions {
  fetchImpl?: typeof fetch;
  maxDepth?: number;
  maxPages?: number;
  timeoutMs?: number;
}

export interface BrokenLink {
  url: string;
  status: number | "timeout" | "error";
  // The page the link was found on; null for the start page itself.
  found_on: string | null;
}

export interface CrawlSummary {
  pages_checked: number;
  broken: BrokenLink[];
  broken_count: number;
  page_limit_reached: boolean;
  robots_skipped: number;
}

// Fifty entries of a long URL each stay under 16 KB with room to spare.
const MAX_BROKEN_LISTED = 50;

interface Queued {
  url: string;
  depth: number;
  foundOn: string | null;
}

type Fetched =
  | { kind: "answered"; status: number; html: string | null }
  | { kind: "timeout" }
  | { kind: "error" };

export async function crawl(start: string, options: CrawlOptions = {}): Promise<CrawlSummary> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const maxDepth = options.maxDepth ?? 3;
  const maxPages = options.maxPages ?? 200;
  const timeoutMs = options.timeoutMs ?? 10_000;

  const origin = new URL(start).origin;
  const allowed = await loadRobots(origin, fetchImpl, timeoutMs);

  const startUrl = normalize(start, start)!;
  const seen = new Set<string>([startUrl]);
  const queue: Queued[] = [{ url: startUrl, depth: 0, foundOn: null }];
  const broken: BrokenLink[] = [];
  let pagesChecked = 0;
  let robotsSkipped = 0;
  let pageLimitReached = false;

  // Breadth first, so the page limit cuts the deepest pages, not a whole branch.
  while (queue.length > 0) {
    const next = queue.shift()!;
    if (!allowed(new URL(next.url).pathname + new URL(next.url).search)) {
      robotsSkipped += 1;
      continue;
    }
    if (pagesChecked >= maxPages) {
      pageLimitReached = true;
      break;
    }
    pagesChecked += 1;

    const fetched = await fetchPage(next.url, fetchImpl, timeoutMs, next.depth < maxDepth);
    if (fetched.kind !== "answered") {
      broken.push({ url: next.url, status: fetched.kind, found_on: next.foundOn });
      continue;
    }
    if (fetched.status >= 400) {
      broken.push({ url: next.url, status: fetched.status, found_on: next.foundOn });
      continue;
    }
    if (fetched.html === null) {
      continue;
    }
    for (const href of extractHrefs(fetched.html)) {
      const url = normalize(href, next.url);
      if (url === null || new URL(url).origin !== origin || seen.has(url)) {
        continue;
      }
      seen.add(url);
      queue.push({ url, depth: next.depth + 1, foundOn: next.url });
    }
  }

  return {
    pages_checked: pagesChecked,
    broken: broken.slice(0, MAX_BROKEN_LISTED),
    broken_count: broken.length,
    page_limit_reached: pageLimitReached,
    robots_skipped: robotsSkipped,
  };
}

async function fetchPage(
  url: string,
  fetchImpl: typeof fetch,
  timeoutMs: number,
  wantLinks: boolean,
): Promise<Fetched> {
  const signal = AbortSignal.timeout(timeoutMs);
  try {
    const response = await fetchImpl(url, {
      headers: { "User-Agent": USER_AGENT },
      redirect: "follow",
      signal,
    });
    const isHtml = (response.headers.get("content-type") ?? "").includes("text/html");
    if (wantLinks && isHtml && response.status < 400) {
      return { kind: "answered", status: response.status, html: await response.text() };
    }
    await response.body?.cancel();
    return { kind: "answered", status: response.status, html: null };
  } catch (error) {
    if (signal.aborted) {
      return { kind: "timeout" };
    }
    return { kind: "error" };
  }
}

function normalize(href: string, base: string): string | null {
  let url: URL;
  try {
    url = new URL(href.trim(), base);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return null;
  }
  url.hash = "";
  return url.href;
}

const ANCHOR_HREF = /<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))/gi;

function extractHrefs(html: string): string[] {
  const hrefs: string[] = [];
  for (const match of html.matchAll(ANCHOR_HREF)) {
    const href = match[1] ?? match[2] ?? match[3];
    if (href) {
      hrefs.push(decodeEntities(href));
    }
  }
  return hrefs;
}

function decodeEntities(text: string): string {
  return text.replace(/&amp;/g, "&").replace(/&#38;/g, "&");
}

// robots.txt: the group naming this crawler if there is one, otherwise the
// `*` group. The longest matching rule wins and Allow wins a tie. A missing
// or unreadable robots.txt allows everything.
async function loadRobots(
  origin: string,
  fetchImpl: typeof fetch,
  timeoutMs: number,
): Promise<(path: string) => boolean> {
  let text: string;
  try {
    const response = await fetchImpl(`${origin}/robots.txt`, {
      headers: { "User-Agent": USER_AGENT },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) {
      await response.body?.cancel();
      return () => true;
    }
    text = await response.text();
  } catch {
    return () => true;
  }

  const rules = parseRobots(text);
  return (path) => robotsAllows(rules, path);
}

export function robotsAllows(rules: Rule[], path: string): boolean {
  let best: { allow: boolean; length: number } | null = null;
  for (const rule of rules) {
    if (!rule.pattern.test(path)) {
      continue;
    }
    if (best === null || rule.length > best.length || (rule.length === best.length && rule.allow)) {
      best = { allow: rule.allow, length: rule.length };
    }
  }
  return best === null || best.allow;
}

export interface Rule {
  allow: boolean;
  pattern: RegExp;
  length: number;
}

export function parseRobots(text: string): Rule[] {
  const groups: { agents: string[]; rules: Rule[] }[] = [];
  let current: { agents: string[]; rules: Rule[] } | null = null;
  let lastWasAgent = false;

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*/, "").trim();
    const colon = line.indexOf(":");
    if (colon === -1) {
      continue;
    }
    const field = line.slice(0, colon).trim().toLowerCase();
    const value = line.slice(colon + 1).trim();
    if (field === "user-agent") {
      if (!lastWasAgent || current === null) {
        current = { agents: [], rules: [] };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
      continue;
    }
    lastWasAgent = false;
    if (current === null || (field !== "allow" && field !== "disallow")) {
      continue;
    }
    // An empty Disallow allows everything, which is the same as no rule.
    if (value === "") {
      continue;
    }
    current.rules.push({ allow: field === "allow", pattern: toPattern(value), length: value.length });
  }

  const own = groups.find((group) => group.agents.includes(USER_AGENT));
  const any = groups.find((group) => group.agents.includes("*"));
  return (own ?? any)?.rules ?? [];
}

function toPattern(rule: string): RegExp {
  const anchored = rule.endsWith("$");
  const body = (anchored ? rule.slice(0, -1) : rule)
    .split("*")
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${body}${anchored ? "$" : ""}`);
}
