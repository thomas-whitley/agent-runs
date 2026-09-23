// A fetch that answers from a queue and records what it was sent, so the
// API client and the worker can be tested with no server.

export interface SentRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface FakeReply {
  status: number;
  body?: unknown;
}

export function fakeFetch(replies: FakeReply[]) {
  const sent: SentRequest[] = [];
  const queue = [...replies];

  const fetchImpl = async (input: string | URL | Request, init?: RequestInit) => {
    sent.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: Object.fromEntries(new Headers(init?.headers).entries()),
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    });
    const reply = queue.shift();
    if (reply === undefined) {
      throw new Error(`no fake reply left for ${String(input)}`);
    }
    const body = reply.body === undefined ? null : JSON.stringify(reply.body);
    return new Response(body, { status: reply.status });
  };

  return { fetchImpl: fetchImpl as typeof fetch, sent };
}
