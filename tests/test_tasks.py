"""The task type registry: one row per type, in code."""

from app.config import PROVIDERS
from app.tasks import TASK_TYPES

EXPECTED_NAMES = {"pytest", "chat", "repo_chore", "site_check", "digest"}


def test_the_five_task_types_are_registered():
    assert set(TASK_TYPES) == EXPECTED_NAMES


def test_no_type_is_public():
    """pytest was public until the worker was found to run its task as code
    beside the worker's secrets. Every type now needs the bearer token."""
    assert all(TASK_TYPES[name].public is False for name in EXPECTED_NAMES)


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


def test_ollama_is_registered_as_an_openai_compatible_provider_at_no_cost():
    ollama = PROVIDERS["ollama"]
    assert ollama.kind == "openai_compatible"
    assert ollama.base_url == "https://ollama.com/v1"
    assert ollama.api_key_env == "OLLAMA_API_KEY"
    assert ollama.model == "gpt-oss:120b"
    assert ollama.usd_per_million_tokens == 0.0


def test_chat_runs_on_ollama_and_the_rest_on_gemini():
    assert TASK_TYPES["chat"].provider == "ollama"
    for name in ("pytest", "repo_chore", "digest"):
        assert TASK_TYPES[name].provider == "gemini", name


def test_every_model_type_falls_back_to_a_different_registered_provider():
    for name in EXPECTED_NAMES - {"site_check"}:
        task_type = TASK_TYPES[name]
        assert task_type.fallback in PROVIDERS, name
        assert task_type.fallback != task_type.provider, name
    assert TASK_TYPES["site_check"].fallback is None
