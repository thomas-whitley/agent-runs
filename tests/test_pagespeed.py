"""The cloud Lighthouse path over the PageSpeed Insights API."""

import json
import urllib.parse

import pytest

from app.pagespeed import CATEGORIES, run_pagespeed, summarize

# The parts of a PageSpeed response the summary reads. lighthouseResult is
# the same Lighthouse result the self hosted worker gets from Lighthouse.
LIGHTHOUSE_RESULT = {
    "lighthouseVersion": "13.0.0",
    "finalDisplayedUrl": "https://example.com/",
    "categories": {
        "performance": {"score": 0.72},
        "accessibility": {"score": 1},
        "best-practices": {"score": None},
        "seo": {"score": 0.9},
    },
    "audits": {
        "largest-contentful-paint": {
            "score": 0.5,
            "scoreDisplayMode": "numeric",
            "numericValue": 3120.4,
        },
        "total-blocking-time": {"score": 1, "scoreDisplayMode": "numeric", "numericValue": 40.6},
        "image-alt": {"score": 0, "scoreDisplayMode": "binary"},
        "document-title": {"score": 1, "scoreDisplayMode": "binary"},
        "diagnostics": {"score": None, "scoreDisplayMode": "informative"},
        "viewport": {"score": 0, "scoreDisplayMode": "notApplicable"},
        "structured-data": {"score": None, "scoreDisplayMode": "manual"},
    },
}


def test_the_summary_has_the_same_shape_the_checks_worker_posts():
    """checks/src/summary.ts writes these keys. A reader of GET /runs should
    not have to know which executor ran the check to read its result."""
    assert summarize(LIGHTHOUSE_RESULT) == {
        "scores": {"performance": 0.72, "accessibility": 1, "best-practices": None, "seo": 0.9},
        "lcp_ms": 3120,
        "tbt_ms": 41,
        "failed_audits": ["image-alt", "largest-contentful-paint"],
        "lighthouse_version": "13.0.0",
        "final_url": "https://example.com/",
    }


def test_the_summary_has_no_timings_when_the_audits_are_missing():
    result = {**LIGHTHOUSE_RESULT, "audits": {}}

    summary = summarize(result)

    assert summary["lcp_ms"] is None
    assert summary["tbt_ms"] is None
    assert summary["failed_audits"] == []


class FakeOpener:
    """Stands in for urllib's urlopen and records the request it was given."""

    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.body


def test_run_pagespeed_asks_for_the_page_and_every_category():
    opener = FakeOpener({"lighthouseResult": LIGHTHOUSE_RESULT})

    run_pagespeed("https://example.com/a?b=1", api_key=None, opener=opener)

    request, _ = opener.requests[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query["url"] == ["https://example.com/a?b=1"]
    assert query["category"] == list(CATEGORIES)
    assert query["strategy"] == ["mobile"]


def test_run_pagespeed_returns_the_summary():
    opener = FakeOpener({"lighthouseResult": LIGHTHOUSE_RESULT})

    assert run_pagespeed("https://example.com/", None, opener=opener) == summarize(
        LIGHTHOUSE_RESULT
    )


def test_the_api_key_goes_in_a_header_and_never_in_the_url():
    """An error or a log line can carry the URL, so the key stays out of it."""
    opener = FakeOpener({"lighthouseResult": LIGHTHOUSE_RESULT})

    run_pagespeed("https://example.com/", api_key="secret-key", opener=opener)

    request, _ = opener.requests[0]
    assert "secret-key" not in request.full_url
    assert request.get_header("X-goog-api-key") == "secret-key"


def test_with_no_api_key_no_key_header_is_sent():
    opener = FakeOpener({"lighthouseResult": LIGHTHOUSE_RESULT})

    run_pagespeed("https://example.com/", api_key=None, opener=opener)

    request, _ = opener.requests[0]
    assert request.get_header("X-goog-api-key") is None


def test_a_response_with_no_lighthouse_result_is_an_error():
    opener = FakeOpener({"error": {"code": 500, "message": "Lighthouse returned error"}})

    with pytest.raises(ValueError, match="Lighthouse returned error"):
        run_pagespeed("https://example.com/", None, opener=opener)


def test_an_http_error_from_pagespeed_reports_its_message(monkeypatch):
    """A refused request, such as a 429 when the daily quota is spent, comes
    back with Google's JSON error. Its message says more than the status."""
    import http.server
    import threading

    class QuotaSpent(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"error": {"code": 429, "message": "Quota exceeded"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), QuotaSpent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        "app.pagespeed.PAGESPEED_URL", f"http://127.0.0.1:{server.server_port}/runPagespeed"
    )
    try:
        with pytest.raises(ValueError, match="Quota exceeded"):
            run_pagespeed("https://example.com/", None)
    finally:
        server.shutdown()
        server.server_close()
