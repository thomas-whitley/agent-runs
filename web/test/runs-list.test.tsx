// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Run, RunsPage } from "../src/api";
import { RunsList } from "../src/RunsList";

afterEach(cleanup);

function run(n: number, overrides: Partial<Run> = {}): Run {
  return {
    id: `${String(n).padStart(8, "0")}-0000-4000-8000-000000000000`,
    type: "site_check",
    provider: null,
    executor: null,
    status: "succeeded",
    tokens: 0,
    duration_seconds: 0.084,
    created_at: `2026-09-28T10:${String(59 - n).padStart(2, "0")}:00+00:00`,
    ...overrides,
  };
}

/**
 * The server's contract from app/main.py: newest first, a next_cursor only
 * when the page came back full, and a cursor that resumes strictly after the
 * last row it was cut from. The cursor here is that row's id.
 */
function fakeServer(rows: Run[]) {
  const calls: Array<{ cursor: string | null; limit: number }> = [];
  async function fetchPage(cursor: string | null, limit: number): Promise<RunsPage> {
    calls.push({ cursor, limit });
    const start = cursor === null ? 0 : rows.findIndex((r) => r.id === cursor) + 1;
    const page = rows.slice(start, start + limit);
    const full = page.length === limit;
    return { runs: page, next_cursor: full ? page[page.length - 1]!.id : null };
  }
  return { fetchPage, calls };
}

function renderedIds(): string[] {
  return screen.getAllByTestId("run-row").map((row) => row.getAttribute("data-run-id")!);
}

describe("RunsList pagination", () => {
  it("shows a next control after a page of exactly limit rows", async () => {
    const server = fakeServer([run(1), run(2), run(3)]);

    render(<RunsList fetchPage={server.fetchPage} pageSize={3} />);

    expect(await screen.findAllByTestId("run-row")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Older runs" })).toBeTruthy();
  });

  it("shows no next control after a page shorter than limit", async () => {
    const server = fakeServer([run(1), run(2)]);

    render(<RunsList fetchPage={server.fetchPage} pageSize={3} />);

    expect(await screen.findAllByTestId("run-row")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Older runs" })).toBeNull();
  });

  it("shows an empty state when the first page is empty", async () => {
    const server = fakeServer([]);

    render(<RunsList fetchPage={server.fetchPage} pageSize={3} />);

    expect(await screen.findByText("No runs yet.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Older runs" })).toBeNull();
  });

  it("asks for the second page with the first page's cursor and never repeats its last row", async () => {
    const rows = [run(1), run(2), run(3), run(4), run(5)];
    const server = fakeServer(rows);

    render(<RunsList fetchPage={server.fetchPage} pageSize={3} />);
    await screen.findAllByTestId("run-row");
    fireEvent.click(screen.getByRole("button", { name: "Older runs" }));
    await screen.findByText(rows[4]!.id.slice(0, 8));

    expect(server.calls).toEqual([
      { cursor: null, limit: 3 },
      { cursor: rows[2]!.id, limit: 3 },
    ]);
    expect(renderedIds()).toEqual(rows.map((r) => r.id));
    expect(screen.queryByRole("button", { name: "Older runs" })).toBeNull();
  });

  it("drops a row the server repeats across a page boundary", async () => {
    const [a, b, c, d] = [run(1), run(2), run(3), run(4)];
    const pages: RunsPage[] = [
      { runs: [a!, b!], next_cursor: "first" },
      { runs: [b!, c!, d!].slice(0, 2), next_cursor: null },
    ];
    let call = 0;

    render(<RunsList fetchPage={async () => pages[call++]!} pageSize={2} />);
    await screen.findAllByTestId("run-row");
    fireEvent.click(screen.getByRole("button", { name: "Older runs" }));
    await screen.findByText(c!.id.slice(0, 8));

    expect(renderedIds()).toEqual([a!.id, b!.id, c!.id]);
  });
});

describe("RunsList row", () => {
  it("shows type, provider, executor, status, tokens and duration", async () => {
    const server = fakeServer([
      run(1, {
        type: "pytest",
        provider: "gemini",
        executor: null,
        status: "running",
        tokens: 1234,
        duration_seconds: null,
      }),
      run(2, { executor: "self_hosted", duration_seconds: 11.8 }),
    ]);

    render(<RunsList fetchPage={server.fetchPage} pageSize={50} />);
    const [first, second] = await screen.findAllByTestId("run-row");

    expect(first!.textContent).toContain("pytest");
    expect(first!.textContent).toContain("gemini");
    expect(first!.textContent).toContain("running");
    expect(first!.textContent).toContain("1,234");
    // A null executor is a run that is not a check; the server writes "cloud" itself.
    expect(first!.textContent).not.toContain("cloud");
    expect(second!.textContent).toContain("site_check");
    expect(second!.textContent).toContain("self_hosted");
    expect(second!.textContent).toContain("11.8 s");
  });

  it("says so when the list cannot be loaded", async () => {
    render(
      <RunsList
        fetchPage={async () => {
          throw new Error("GET /runs answered 503");
        }}
        pageSize={50}
      />,
    );

    expect(await screen.findByText("Could not load runs: GET /runs answered 503")).toBeTruthy();
  });
});
