"""Loads only what step 2a reads from mercury.yaml: the portfolio's sites.

The full schema (providers, tasks, budgets, telegram, retention) belongs to
later steps; parsing sections nothing calls yet is code with no caller.
"""

import pytest
import yaml

from app.mercury_config import load_mercury_config


def test_load_mercury_config_reads_the_site_list(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(
        yaml.dump({"portfolio": {"sites": ["https://a.example", "https://b.example"]}})
    )

    config = load_mercury_config(config_file)

    assert config.sites == ("https://a.example", "https://b.example")


def test_load_mercury_config_defaults_to_no_sites_when_the_section_is_missing(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(yaml.dump({"telegram": {"chat_id": 1}}))

    config = load_mercury_config(config_file)

    assert config.sites == ()


def test_load_mercury_config_raises_when_the_file_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_mercury_config(tmp_path / "does-not-exist.yaml")


def test_load_mercury_config_reads_the_pages_for_weekly_browser_checks(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(
        yaml.dump(
            {"portfolio": {"sites": ["https://a.example/health"], "pages": ["https://a.example/"]}}
        )
    )

    config = load_mercury_config(config_file)

    assert config.pages == ("https://a.example/",)


def test_load_mercury_config_defaults_to_no_pages(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(yaml.dump({"portfolio": {"sites": ["https://a.example"]}}))

    assert load_mercury_config(config_file).pages == ()


def test_load_mercury_config_reads_the_telegram_chat_id(tmp_path):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(yaml.dump({"telegram": {"chat_id": 123456789}}))

    assert load_mercury_config(config_file).telegram_chat_id == 123456789


@pytest.mark.parametrize("telegram", [{"chat_id": 0}, {}, None])
def test_a_zero_or_missing_chat_id_allows_no_chat(tmp_path, telegram):
    config_file = tmp_path / "mercury.yaml"
    config_file.write_text(yaml.dump({"telegram": telegram}))

    assert load_mercury_config(config_file).telegram_chat_id is None
