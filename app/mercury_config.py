"""Loads the parts of mercury.yaml the scheduler reads: the portfolio's sites,
checked hourly for uptime, and its pages, checked weekly with Lighthouse and
the broken link crawl. The full schema arrives with the steps that read the
rest of it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MercuryConfig:
    sites: tuple[str, ...]
    pages: tuple[str, ...] = ()


def load_mercury_config(path: str | Path) -> MercuryConfig:
    """Read mercury.yaml. Raises FileNotFoundError if it is not there,
    which is what a scheduler started with no config mounted should do."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    portfolio = data.get("portfolio") or {}
    return MercuryConfig(
        sites=tuple(portfolio.get("sites") or []),
        pages=tuple(portfolio.get("pages") or []),
    )
