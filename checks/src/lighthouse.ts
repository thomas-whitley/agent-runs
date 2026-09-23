// The real Lighthouse run, in headless Chrome on this machine. Only this file
// touches Chrome; the tests stub it out.

import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";

import type { LighthouseResult } from "./summary.js";

export async function runLighthouse(url: string): Promise<LighthouseResult> {
  const chrome = await chromeLauncher.launch({ chromeFlags: ["--headless=new"] });
  try {
    const run = await lighthouse(url, { port: chrome.port, output: "json", logLevel: "error" });
    if (run === undefined) {
      throw new Error(`lighthouse returned nothing for ${url}`);
    }
    return run.lhr as unknown as LighthouseResult;
  } finally {
    chrome.kill();
  }
}
