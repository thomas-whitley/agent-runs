import { fetchRunsPage } from "./api";
import { RunsList } from "./RunsList";

export function App() {
  return (
    <main>
      <h1>agent-runs</h1>
      <p className="lede">Every run the API has taken, newest first. Metadata only.</p>
      <RunsList fetchPage={fetchRunsPage} pageSize={50} />
    </main>
  );
}
