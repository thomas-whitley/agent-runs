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


def test_the_check_claim_window_defaults_to_thirty_minutes(monkeypatch):
    monkeypatch.delenv("CHECK_CLAIM_WINDOW", raising=False)

    assert load_settings().check_claim_window_seconds == 1800.0


def test_the_check_claim_window_is_configurable_in_seconds(monkeypatch):
    monkeypatch.setenv("CHECK_CLAIM_WINDOW", "300")

    assert load_settings().check_claim_window_seconds == 300.0


def test_claim_next_run_defaults_to_the_same_check_claim_window(monkeypatch):
    monkeypatch.delenv("CHECK_CLAIM_WINDOW", raising=False)
    parameters = inspect.signature(claim_next_run).parameters
    default = parameters["check_claim_window_seconds"].default

    assert default == load_settings().check_claim_window_seconds


def test_the_pagespeed_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "psi-key")

    assert load_settings().pagespeed_api_key == "psi-key"


def test_an_empty_pagespeed_key_counts_as_none(monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "")

    assert load_settings().pagespeed_api_key is None
