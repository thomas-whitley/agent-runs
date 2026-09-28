// A same origin site in memory, served through a fetch the crawl is given,
// recording every URL it was asked for.

export interface FakePage {
  status?: number;
  html?: string;
  contentType?: string;
  // Never answers until the request's signal aborts.
  hang?: boolean;
  // Fails the way fetch does when a connection is refused.
  refuse?: boolean;
}

export function fakeSite(pages: Record<string, FakePage>) {
  const requested: string[] = [];

  const fetchImpl = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requested.push(url);
    const page = pages[url];
    if (page?.refuse) {
      throw new TypeError("fetch failed");
    }
    if (page?.hang) {
      return new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
      });
    }
    if (page === undefined) {
      return new Response("not found", { status: 404, headers: { "Content-Type": "text/plain" } });
    }
    const status = page.status ?? 200;
    // A null body status cannot carry a body, even an empty one.
    const body = status === 204 || status === 304 ? null : (page.html ?? "");
    return new Response(body, {
      status,
      headers: { "Content-Type": page.contentType ?? "text/html; charset=utf-8" },
    });
  };

  return { fetchImpl: fetchImpl as typeof fetch, requested };
}

export function links(...hrefs: string[]): string {
  return `<html><body>${hrefs.map((href) => `<a href="${href}">x</a>`).join("")}</body></html>`;
}
