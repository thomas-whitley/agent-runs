// The three endpoints in app/check_claims.py, over HTTPS with the bearer
// token. This machine never holds a database credential, so the token is
// the one secret it has.
//
// A 409 from heartbeat or result means another worker took the check over
// or it is already closed. That is an answer, not an error: the worker
// stops and moves on.

export interface ClaimedCheck {
  id: string;
  url: string;
  kind: string;
  lease_seconds: number;
}

export type Held = "held" | "lost";
export type Closed = "closed" | "lost";

export class ApiError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
  ) {
    super(`${path} answered ${status}`);
    this.name = "ApiError";
  }
}

export interface ApiOptions {
  baseUrl: string;
  token: string;
  workerId: string;
  fetchImpl?: typeof fetch;
}

export class ChecksApi {
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: ApiOptions) {
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async claim(kinds: string[]): Promise<ClaimedCheck | null> {
    const response = await this.post("/checks/claim", { kinds });
    if (response.status === 204) {
      return null;
    }
    return (await response.json()) as ClaimedCheck;
  }

  async heartbeat(id: string): Promise<Held> {
    const response = await this.post(`/checks/${id}/heartbeat`, {}, [409]);
    return response.status === 409 ? "lost" : "held";
  }

  async postResult(id: string, result: Record<string, unknown>): Promise<Closed> {
    const response = await this.post(`/checks/${id}/result`, { result }, [409]);
    return response.status === 409 ? "lost" : "closed";
  }

  private async post(path: string, body: object, allowed: number[] = []): Promise<Response> {
    const response = await this.fetchImpl(`${this.options.baseUrl}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.options.token}`,
      },
      body: JSON.stringify({ worker_id: this.options.workerId, ...body }),
      // The API scales to zero, so this covers a cold start, as the scheduler's does.
      signal: AbortSignal.timeout(60_000),
    });
    if (!response.ok && !allowed.includes(response.status)) {
      throw new ApiError(path, response.status);
    }
    return response;
  }
}
