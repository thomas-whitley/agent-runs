"""The cloud Lighthouse path: a lighthouse check no self hosted worker claimed
within the window runs over the PageSpeed Insights API, per docs/mercury.md.

The summary is the one checks/src/summary.ts writes, so GET /runs reads the
same whichever executor ran the check. It asks for the four categories the
PageSpeed API documents, where a local Lighthouse 13 run also scores
agentic-browsing.

Without an API key the requests share a quota with every other keyless
caller, and on 2026-09-24 that quota was already spent, so in practice the
fallback needs PAGESPEED_API_KEY.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CATEGORIES = ("performance", "accessibility", "best-practices", "seo")

# PageSpeed usually answers in 10 to 40 seconds. This stays under the two
# minute lease, since the worker cannot heartbeat during the call.
PAGESPEED_TIMEOUT_SECONDS = 90.0

# Lighthouse's own report shows an audit as passed at 0.9 and above.
PASS_THRESHOLD = 0.9

# Audits with these modes carry no pass or fail, so they are never "failed".
UNSCORED_MODES = frozenset({"informative", "manual", "notApplicable", "error"})

Opener = Callable[[urllib.request.Request, float], bytes]


def _milliseconds(audit: dict[str, Any] | None) -> int | None:
    if audit is None or audit.get("numericValue") is None:
        return None
    return round(audit["numericValue"])


def _failed(audit: dict[str, Any]) -> bool:
    if audit.get("score") is None or audit.get("scoreDisplayMode") in UNSCORED_MODES:
        return False
    return audit["score"] < PASS_THRESHOLD


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """About 1 KB of a result that is several hundred. The rest is discarded."""
    audits = result["audits"]
    return {
        "scores": {name: category["score"] for name, category in result["categories"].items()},
        "lcp_ms": _milliseconds(audits.get("largest-contentful-paint")),
        "tbt_ms": _milliseconds(audits.get("total-blocking-time")),
        "failed_audits": sorted(name for name, audit in audits.items() if _failed(audit)),
        "lighthouse_version": result["lighthouseVersion"],
        "final_url": result["finalDisplayedUrl"],
    }


def _urlopen(request: urllib.request.Request, timeout: float) -> bytes:
    """A refused request still has a JSON body naming why, such as the daily
    quota being spent, so it is returned for run_pagespeed to report."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.read()


def run_pagespeed(
    url: str,
    api_key: str | None,
    opener: Opener = _urlopen,
    timeout_seconds: float = PAGESPEED_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run Lighthouse on url through PageSpeed and return the summary.

    The key goes in the X-Goog-Api-Key header, never the query string, so an
    error message or a log line that carries the URL cannot carry the key.
    With no key PageSpeed still answers, under a smaller shared quota.
    """
    query = urllib.parse.urlencode(
        [("url", url), ("strategy", "mobile"), *(("category", name) for name in CATEGORIES)]
    )
    headers = {"X-Goog-Api-Key": api_key} if api_key else {}
    request = urllib.request.Request(f"{PAGESPEED_URL}?{query}", headers=headers)
    body = json.loads(opener(request, timeout_seconds))
    if "lighthouseResult" not in body:
        message = body.get("error", {}).get("message", "no lighthouseResult in the response")
        raise ValueError(f"PageSpeed returned no result: {message}")
    return summarize(body["lighthouseResult"])
