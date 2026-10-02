/** One row of GET /runs, as app/run_list.py serialize_run_row writes it. */
export interface Run {
  id: string;
  type: string;
  provider: string | null;
  executor: string | null;
  status: string;
  tokens: number;
  duration_seconds: number | null;
  created_at: string;
  /** telegram, mcp, n8n, api or scheduler: who asked for the run. */
  source: string;
}

export interface RunsPage {
  runs: Run[];
  next_cursor: string | null;
}

export type FetchPage = (cursor: string | null, limit: number) => Promise<RunsPage>;

/** GET /runs on the same origin that served the page. */
export const fetchRunsPage: FetchPage = async (cursor, limit) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) params.set("cursor", cursor);
  const response = await fetch(`/runs?${params}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`GET /runs answered ${response.status}`);
  return (await response.json()) as RunsPage;
};
