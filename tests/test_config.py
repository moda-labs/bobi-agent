"""Tests for machine config loading from ~/.modastack/config.yaml."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from modastack.config import Config, load_deployment_state, save_deployment_state


def test_loads_machine_config(tmp_path):
    machine_yaml = tmp_path / "config.yaml"
    machine_yaml.write_text(dedent("""
        event_server:
          url: https://events.example.com
        slack:
          bot_token: xoxb-test
        linear:
          api_key: lin_api_test
    """))

    with patch("modastack.config._machine_config_path", return_value=machine_yaml):
        cfg = Config.load()

    assert cfg.event_server_url == "https://events.example.com"
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.linear_api_key == "lin_api_test"


def test_defaults_when_no_config():
    with patch("modastack.config._machine_config_path", return_value=Path("/nonexistent")):
        cfg = Config.load()

    assert cfg.event_server_url == ""
    assert cfg.slack_bot_token == ""
    assert cfg.linear_api_key == ""


def test_from_file_alias():
    with patch("modastack.config._machine_config_path", return_value=Path("/nonexistent")):
        cfg = Config.from_file(Path("/tmp"))
    assert cfg.event_server_url == ""


# --- Deployment state (ephemeral) ---


def test_deployment_state_roundtrip(tmp_path):
    state_dir = tmp_path / ".modastack" / "state"
    state_dir.mkdir(parents=True)

    save_deployment_state(tmp_path, "dep-123", "moda_key456")
    state = load_deployment_state(tmp_path)

    assert state["deployment_id"] == "dep-123"
    assert state["api_key"] == "moda_key456"


def test_deployment_state_missing_returns_empty(tmp_path):
    state = load_deployment_state(tmp_path)
    assert state == {}
