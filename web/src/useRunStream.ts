import { useCallback, useEffect, useRef, useState } from "react";

/** The part of the browser's EventSource this hook uses, so tests can fake it. */
export interface EventSourceLike {
  addEventListener(name: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export type OpenSource = (url: string) => EventSourceLike;

export const openEventSource: OpenSource = (url) => new EventSource(url);

/** One event as the page shows it. Without a token the API sends no bodies,
 * only the step's number and kind, and the done event's status. */
export interface StreamEvent {
  id: number;
  seq: number | null;
  kind: string;
  status: string | null;
  receivedAt: number;
}

export type Connection = "open" | "dropped" | "finished";

/**
 * Streams /runs/{id}/events, merging events by their monotonic id so a
 * resumed or retried connection never shows a row twice. kill() closes the
 * connection the way a dropped network would; reconnect() opens a new one
 * with ?after= set to the last id seen, because an EventSource cannot set
 * Last-Event-ID itself on a fresh connection.
 */
export function useRunStream(runId: string, openSource: OpenSource = openEventSource) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connection, setConnection] = useState<Connection>("open");
  const [drops, setDrops] = useState<number[]>([]);
  const source = useRef<EventSourceLike | null>(null);
  const lastId = useRef(0);

  const connect = useCallback(() => {
    const url =
      lastId.current > 0
        ? `/runs/${runId}/events?after=${lastId.current}`
        : `/runs/${runId}/events`;
    const opened = openSource(url);
    source.current = opened;
    setConnection("open");

    const receive = (message: MessageEvent) => {
      const id = Number(message.lastEventId);
      const data = JSON.parse(message.data) as {
        seq?: number;
        kind?: string;
        output?: { status?: string };
      };
      if (id > lastId.current) lastId.current = id;
      setEvents((current) => {
        if (current.some((e) => e.id === id)) return current;
        const event: StreamEvent = {
          id,
          seq: data.seq ?? null,
          kind: data.kind ?? message.type,
          status: data.output?.status ?? null,
          receivedAt: Date.now(),
        };
        return [...current, event].sort((a, b) => a.id - b.id);
      });
      if (message.type === "done") {
        opened.close();
        setConnection("finished");
      }
    };
    opened.addEventListener("step", receive);
    opened.addEventListener("done", receive);
  }, [runId, openSource]);

  useEffect(() => {
    lastId.current = 0;
    setEvents([]);
    setDrops([]);
    connect();
    return () => source.current?.close();
  }, [connect]);

  const kill = useCallback(() => {
    source.current?.close();
    setDrops((current) => [...current, lastId.current]);
    setConnection("dropped");
  }, []);

  return { events, connection, drops, kill, reconnect: connect };
}
