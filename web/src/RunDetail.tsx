import { Fragment } from "react";

import { type OpenSource, openEventSource, useRunStream } from "./useRunStream";

/** One run's steps as they arrive, with a button that drops the connection
 * and one that resumes it from the last event seen. */
export function RunDetail({
  runId,
  openSource = openEventSource,
}: {
  runId: string;
  openSource?: OpenSource;
}) {
  const { events, connection, drops, kill, reconnect } = useRunStream(runId, openSource);
  const first = events[0]?.receivedAt;
  const done = events.find((e) => e.kind === "done");

  return (
    <section>
      <p>
        <a href="#">All runs</a>
      </p>
      <h2 className="mono">{runId}</h2>
      <p className="lede">
        {connection === "open" && <span className="status status-running">live</span>}
        {connection === "dropped" && <span className="status status-failed">disconnected</span>}
        {done?.status && <span className={`status status-${done.status}`}>{done.status}</span>}{" "}
        Step names only. The code and test output in each step need the API's bearer token.
      </p>
      <p>
        {connection === "open" && (
          <button type="button" onClick={kill}>
            Kill connection
          </button>
        )}
        {connection === "dropped" && (
          <button type="button" onClick={reconnect}>
            Reconnect
          </button>
        )}
      </p>
      <table>
        <thead>
          <tr>
            <th scope="col">Event</th>
            <th scope="col">Step</th>
            <th scope="col">Kind</th>
            <th scope="col" className="num">
              Arrived
            </th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <Fragment key={event.id}>
              <tr data-testid="event-row" data-event-id={event.id}>
                <td className="mono">#{event.id}</td>
                <td>{event.seq ?? ""}</td>
                <td>{event.kind}</td>
                <td className="num">
                  {first === undefined ? "" : `+${((event.receivedAt - first) / 1000).toFixed(1)} s`}
                </td>
              </tr>
              {drops
                .filter((id) => id === event.id)
                .slice(0, 1)
                .map((id) => (
                  <tr key={`drop-${id}`} className="drop">
                    <td colSpan={4}>
                      Connection dropped after #{id}. Reconnect asks for everything after it.
                    </td>
                  </tr>
                ))}
            </Fragment>
          ))}
        </tbody>
      </table>
      {events.length === 0 && connection !== "finished" && (
        <p className="note">Waiting for the first event…</p>
      )}
    </section>
  );
}
