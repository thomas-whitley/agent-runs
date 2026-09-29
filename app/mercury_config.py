"""Loads the parts of mercury.yaml that are read: the portfolio's sites,
checked hourly for uptime, its pages, checked weekly with Lighthouse and the
broken link crawl, its repos, which a repo_chore may touch, and the one
Telegram chat the bot answers. The full schema arrives with the steps that
read the rest of it.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RepoConfig:
    """owner/name, and the command that runs its tests from the repo root.
    A repo with no test command cannot have a chore, since a chore opens a
    pull request only when the tests pass."""

    name: str
    test_command: str | None = None


@dataclass(frozen=True)
class MercuryConfig:
    sites: tuple[str, ...]
    pages: tuple[str, ...] = ()
    # The one chat the bot answers. None, including the sample's 0, answers no one.
    telegram_chat_id: int | None = None
    repos: tuple[RepoConfig, ...] = ()


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
        repos=tuple(_repo(entry) for entry in portfolio.get("repos") or []),
    )


def _repo(entry: str | dict) -> RepoConfig:
    if isinstance(entry, str):
        return RepoConfig(name=entry)
    return RepoConfig(name=entry["name"], test_command=entry.get("test_command"))
