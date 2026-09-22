"""The cloud side check: plain HTTP status and latency, no browser, no
model call. Uptime and the CI failure watch stay here rather than on the
self hosted worker, per docs/mercury.md, because they should not depend on
a machine that sleeps.
"""

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class SiteCheckResult:
    url: str
    status_code: int | None
    latency_ms: float | None
    passed: bool
    error: str | None = None


def check_site(url: str, timeout_seconds: float = 10.0) -> SiteCheckResult:
    """A 2xx within the timeout passes; redirects are followed first, so the
    status seen is the final hop's. Anything else, including a
    timeout or a connection error, is a finding, not an exception."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return SiteCheckResult(
                url=url,
                status_code=response.status,
                latency_ms=(time.monotonic() - started) * 1000,
                passed=response.status < 400,
            )
    except urllib.error.HTTPError as error:
        return SiteCheckResult(
            url=url,
            status_code=error.code,
            latency_ms=(time.monotonic() - started) * 1000,
            passed=error.code < 400,
        )
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        return SiteCheckResult(
            url=url,
            status_code=None,
            latency_ms=(time.monotonic() - started) * 1000,
            passed=False,
            error=str(error),
        )
