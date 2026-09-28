// Headless always. CHROME_EXTRA_FLAGS adds to it, which the container in
// checks/Dockerfile uses for --no-sandbox: Chrome's own sandbox needs kernel
// privileges Docker does not grant by default, so the container is the
// boundary there instead.
export function chromeFlags(extra: string | undefined = process.env.CHROME_EXTRA_FLAGS): string[] {
  return ["--headless=new", ...(extra ?? "").split(/\s+/).filter(Boolean)];
}
