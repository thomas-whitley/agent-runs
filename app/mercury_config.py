"""Loads the parts of mercury.yaml step 2a actually reads: the portfolio's
sites. The full schema arrives with the steps that read the rest of it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MercuryConfig:
    sites: tuple[str, ...]


def load_mercury_config(path: str | Path) -> MercuryConfig:
    """Read mercury.yaml. Raises FileNotFoundError if it is not there,
    which is what a scheduler started with no config mounted should do."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    portfolio = data.get("portfolio") or {}
    return MercuryConfig(sites=tuple(portfolio.get("sites") or []))
