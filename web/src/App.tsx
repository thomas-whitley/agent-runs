import { useEffect, useState } from "react";

import { fetchRunsPage } from "./api";
import { RunDetail } from "./RunDetail";
import { RunsList } from "./RunsList";

/** #/runs/<id> opens one run; anything else is the list. A hash route, so no
 * path ever competes with the API's own /runs routes. */
function runIdFromHash(): string | null {
  if (typeof window === "undefined") return null;
  const match = /^#\/runs\/([0-9a-f-]{36})$/.exec(window.location.hash);
  return match ? match[1]! : null;
}

export function App() {
  const [runId, setRunId] = useState(runIdFromHash);

  useEffect(() => {
    const onChange = () => setRunId(runIdFromHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return (
    <main>
      <h1>Mercury</h1>
      {runId === null ? (
        <>
          <p className="lede">
            Every run the API has taken, newest first. Open one to watch its steps arrive, drop the
            connection, and resume.
          </p>
          <RunsList fetchPage={fetchRunsPage} pageSize={50} />
        </>
      ) : (
        <RunDetail key={runId} runId={runId} />
      )}
    </main>
  );
}
