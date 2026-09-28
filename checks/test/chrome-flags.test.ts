import { describe, expect, it } from "vitest";

import { chromeFlags } from "../src/chrome-flags.js";

describe("chromeFlags", () => {
  it("runs headless with nothing else by default", () => {
    expect(chromeFlags(undefined)).toEqual(["--headless=new"]);
  });

  it("adds the space separated flags from CHROME_EXTRA_FLAGS", () => {
    expect(chromeFlags(" --no-sandbox  --disable-dev-shm-usage ")).toEqual([
      "--headless=new",
      "--no-sandbox",
      "--disable-dev-shm-usage",
    ]);
  });
});
