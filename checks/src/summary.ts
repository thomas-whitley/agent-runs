// A Lighthouse result is several hundred KB. What the API keeps is about
// 1 KB of it: the five category scores, largest contentful paint, total
// blocking time and the ids of the audits that failed. The full report is
// discarded, per docs/mercury.md.

// Lighthouse's own report shows an audit as passed at 0.9 and above.
const PASS_THRESHOLD = 0.9;

// Audits with these modes carry no pass or fail, so they are never "failed".
const UNSCORED_MODES = new Set(["informative", "manual", "notApplicable", "error"]);

interface Audit {
  score: number | null;
  scoreDisplayMode?: string;
  numericValue?: number;
}

// The parts of a Lighthouse result this reads, so a test can build one.
export interface LighthouseResult {
  lighthouseVersion: string;
  finalDisplayedUrl: string;
  categories: Record<string, { score: number | null }>;
  audits: Record<string, Audit>;
}

export interface LighthouseSummary {
  scores: Record<string, number | null>;
  lcp_ms: number | null;
  tbt_ms: number | null;
  failed_audits: string[];
  lighthouse_version: string;
  final_url: string;
}

function milliseconds(audit: Audit | undefined): number | null {
  return audit?.numericValue === undefined ? null : Math.round(audit.numericValue);
}

function failed(audit: Audit): boolean {
  if (audit.score === null || UNSCORED_MODES.has(audit.scoreDisplayMode ?? "")) {
    return false;
  }
  return audit.score < PASS_THRESHOLD;
}

export function summarize(result: LighthouseResult): LighthouseSummary {
  const scores: Record<string, number | null> = {};
  for (const [id, category] of Object.entries(result.categories)) {
    scores[id] = category.score;
  }

  return {
    scores,
    lcp_ms: milliseconds(result.audits["largest-contentful-paint"]),
    tbt_ms: milliseconds(result.audits["total-blocking-time"]),
    failed_audits: Object.entries(result.audits)
      .filter(([, audit]) => failed(audit))
      .map(([id]) => id)
      .sort(),
    lighthouse_version: result.lighthouseVersion,
    final_url: result.finalDisplayedUrl,
  };
}
