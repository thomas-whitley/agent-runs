"""The task type registry: one row per type, in code."""

from app.config import PROVIDERS
from app.tasks import TASK_TYPES

EXPECTED_NAMES = {"pytest", "chat", "repo_chore", "site_check", "digest"}


def test_the_five_task_types_are_registered():
    assert set(TASK_TYPES) == EXPECTED_NAMES


def test_pytest_is_the_only_public_type():
    assert TASK_TYPES["pytest"].public is True
    assert all(TASK_TYPES[name].public is False for name in EXPECTED_NAMES if name != "pytest")


def test_site_check_makes_no_model_call():
    assert TASK_TYPES["site_check"].provider is None
    assert TASK_TYPES["site_check"].budget_tokens == 0


def test_every_other_type_names_a_registered_provider():
    for name in EXPECTED_NAMES - {"site_check"}:
        provider = TASK_TYPES[name].provider
        assert provider in PROVIDERS, f"{name} names provider {provider!r}, not in PROVIDERS"


def test_pytest_keeps_its_current_budget():
    """50000 is the token budget the loop already runs with. Registering it must not change it."""
    assert TASK_TYPES["pytest"].budget_tokens == 50_000


def test_providers_map_a_name_to_its_connection_details():
    assert PROVIDERS["gemini"].kind == "openai_compatible"
    assert PROVIDERS["gemini"].base_url
    assert PROVIDERS["gemini"].api_key_env == "MODEL_API_KEY"

    assert PROVIDERS["haiku"].kind == "anthropic"
    assert PROVIDERS["haiku"].api_key_env == "ANTHROPIC_API_KEY"
