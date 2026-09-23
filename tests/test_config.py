"""Settings defaults that other parts of the system depend on."""

import inspect

from app.config import load_settings
from app.worker import claim_next_run


def test_the_lease_defaults_to_two_minutes(monkeypatch):
    """docs/mercury.md: heartbeats every 30 seconds against a two minute lease.
    A model step can run up to 112 seconds between heartbeats (4 attempts of
    25 seconds plus 2, 4 and 6 second waits), so 60 was too short."""
    monkeypatch.delenv("LEASE_SECONDS", raising=False)

    assert load_settings().lease_seconds == 120.0


def test_the_lease_is_configurable(monkeypatch):
    monkeypatch.setenv("LEASE_SECONDS", "45")

    assert load_settings().lease_seconds == 45.0


def test_claim_next_run_defaults_to_the_same_lease(monkeypatch):
    monkeypatch.delenv("LEASE_SECONDS", raising=False)
    default = inspect.signature(claim_next_run).parameters["lease_seconds"].default

    assert default == load_settings().lease_seconds
