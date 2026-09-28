import { useCallback, useEffect, useState } from "react";

import type { FetchPage, Run } from "./api";
import { formatCreated, formatDuration, formatTokens } from "./format";

type Load = { state: "loading" } | { state: "idle" } | { state: "failed"; message: string };

/** Newest first, one page at a time, following the cursor GET /runs hands back. */
export function RunsList({ fetchPage, pageSize }: { fetchPage: FetchPage; pageSize: number }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [load, setLoad] = useState<Load>({ state: "loading" });

  const append = useCallback((page: Run[]) => {
    // Keyset pagination should never repeat a row; this makes sure of it
    // even if two runs share a created_at and the server's tie break moves.
    setRuns((current) => {
      const seen = new Set(current.map((r) => r.id));
      return [...current, ...page.filter((r) => !seen.has(r.id))];
    });
  }, []);

  const loadPage = useCallback(
    async (after: string | null, isCurrent: () => boolean = () => true) => {
      setLoad({ state: "loading" });
      try {
        const page = await fetchPage(after, pageSize);
        if (!isCurrent()) return;
        append(page.runs);
        setCursor(page.next_cursor);
        setLoad({ state: "idle" });
      } catch (error) {
        if (!isCurrent()) return;
        setLoad({ state: "failed", message: error instanceof Error ? error.message : String(error) });
      }
    },
    [fetchPage, pageSize, append],
  );

  useEffect(() => {
    let current = true;
    setRuns([]);
    void loadPage(null, () => current);
    return () => {
      current = false;
    };
  }, [loadPage]);

  const loaded = load.state !== "loading" || runs.length > 0;

  return (
    <section>
      {loaded && runs.length === 0 && load.state === "idle" && <p className="note">No runs yet.</p>}
      {runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Created</th>
              <th scope="col">Type</th>
              <th scope="col">Provider</th>
              <th scope="col">Executor</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Tokens
              </th>
              <th scope="col" className="num">
                Duration
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} data-testid="run-row" data-run-id={run.id}>
                <td className="mono" title={run.id}>
                  {run.id.slice(0, 8)}
                </td>
                <td>{formatCreated(run.created_at)}</td>
                <td>{run.type}</td>
                <td>{run.provider ?? <span className="muted">none</span>}</td>
                <td>{run.executor ?? <span className="muted">none</span>}</td>
                <td>
                  <span className={`status status-${run.status}`}>{run.status}</span>
                </td>
                <td className="num">{formatTokens(run.tokens)}</td>
                <td className="num">{formatDuration(run.duration_seconds, run.status)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {load.state === "loading" && <p className="note">Loading runs…</p>}
      {load.state === "failed" && <p className="note error">Could not load runs: {load.message}</p>}
      {load.state === "idle" && cursor !== null && (
        <button type="button" onClick={() => void loadPage(cursor)}>
          Older runs
        </button>
      )}
    </section>
  );
}
