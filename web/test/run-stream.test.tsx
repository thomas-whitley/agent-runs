// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RunDetail } from "../src/RunDetail";
import type { EventSourceLike } from "../src/useRunStream";

afterEach(cleanup);

const RUN = "0da29195-743d-410b-89df-e973a7eb7b64";

/** Stands in for the browser's EventSource: records each connection and lets
 * a test deliver named events on it, as the API's SSE stream would. */
class FakeSource implements EventSourceLike {
  static opened: FakeSource[] = [];
  closed = false;
  private listeners = new Map<string, Array<(e: MessageEvent) => void>>();

  constructor(readonly url: string) {
    FakeSource.opened.push(this);
  }

  addEventListener(name: string, listener: (e: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  send(id: number, name: "step" | "done", data: object) {
    const event = new MessageEvent(name, { data: JSON.stringify(data), lastEventId: String(id) });
    act(() => this.listeners.get(name)?.forEach((listener) => listener(event)));
  }
}

function open(url: string) {
  return new FakeSource(url);
}

function shown(): string[] {
  return screen.getAllByTestId("event-row").map((row) => row.getAttribute("data-event-id")!);
}

function latest(): FakeSource {
  return FakeSource.opened[FakeSource.opened.length - 1]!;
}

describe("RunDetail stream", () => {
  afterEach(() => {
    FakeSource.opened = [];
  });

  it("opens the run's event stream from the start", () => {
    render(<RunDetail runId={RUN} openSource={open} />);

    expect(FakeSource.opened.map((s) => s.url)).toEqual([`/runs/${RUN}/events`]);
  });

  it("merges events across a killed connection and a resume, in id order", () => {
    render(<RunDetail runId={RUN} openSource={open} />);
    latest().send(211, "step", { seq: 1, kind: "plan" });
    latest().send(212, "step", { seq: 2, kind: "retrieve" });

    fireEvent.click(screen.getByRole("button", { name: "Kill connection" }));
    const first = latest();
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    latest().send(213, "step", { seq: 3, kind: "act" });
    latest().send(214, "done", { seq: 4, kind: "done", output: { status: "succeeded" } });

    expect(first.closed).toBe(true);
    expect(latest().url).toBe(`/runs/${RUN}/events?after=212`);
    expect(shown()).toEqual(["211", "212", "213", "214"]);
    expect(screen.getByText(/dropped after #212/)).toBeTruthy();
    expect(screen.getByText("succeeded")).toBeTruthy();
  });

  it("shows an event the server sends twice only once", () => {
    render(<RunDetail runId={RUN} openSource={open} />);
    latest().send(211, "step", { seq: 1, kind: "plan" });
    latest().send(212, "step", { seq: 2, kind: "retrieve" });

    fireEvent.click(screen.getByRole("button", { name: "Kill connection" }));
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    // A replica that resumed one event early, or the browser's own retry.
    latest().send(212, "step", { seq: 2, kind: "retrieve" });
    latest().send(213, "step", { seq: 3, kind: "act" });

    expect(shown()).toEqual(["211", "212", "213"]);
  });

  it("closes the stream on done so the browser does not reconnect forever", () => {
    render(<RunDetail runId={RUN} openSource={open} />);

    latest().send(5, "done", { seq: 1, kind: "done", output: { status: "failed" } });

    expect(latest().closed).toBe(true);
    expect(screen.queryByRole("button", { name: "Kill connection" })).toBeNull();
  });

  it("reconnects from the start when nothing arrived before the kill", () => {
    render(<RunDetail runId={RUN} openSource={open} />);

    fireEvent.click(screen.getByRole("button", { name: "Kill connection" }));
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));

    expect(latest().url).toBe(`/runs/${RUN}/events`);
  });
});
