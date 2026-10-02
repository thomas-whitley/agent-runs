import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

describe("App", () => {
  it("renders the page title", () => {
    const html = renderToString(<App />);

    expect(html).toContain("<h1>Mercury</h1>");
  });
});
