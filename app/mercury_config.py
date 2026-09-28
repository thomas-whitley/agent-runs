"""Loads the parts of mercury.yaml that are read: the portfolio's sites,
checked hourly for uptime, its pages, checked weekly with Lighthouse and the
broken link crawl, and the one Telegram chat the bot answers. The full
schema arrives with the steps that read the rest of it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MercuryConfig:
    sites: tuple[str, ...]
    pages: tuple[str, ...] = ()
    # The one chat the bot answers. None, including the sample's 0, answers no one.
    telegram_chat_id: int | None = None


def load_mercury_config(path: str | Path) -> MercuryConfig:
    """Read mercury.yaml. Raises FileNotFoundError if it is not there,
    which is what a scheduler started with no config mounted should do."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    portfolio = data.get("portfolio") or {}
    chat_id = (data.get("telegram") or {}).get("chat_id")
    return MercuryConfig(
        sites=tuple(portfolio.get("sites") or []),
        pages=tuple(portfolio.get("pages") or []),
        telegram_chat_id=int(chat_id) if chat_id else None,
    )
