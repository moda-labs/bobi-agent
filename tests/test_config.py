"""Tests for per-project config loading from .modastack/config.yaml."""

from pathlib import Path
from textwrap import dedent

from modastack.config import Config, load_deployment_state, save_deployment_state


def test_loads_project_config(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        event_server:
          url: https://events.example.com
        slack:
          bot_token: xoxb-test
        linear:
          api_key: lin_api_test
    """))

    cfg = Config.load(tmp_path)

    assert cfg.event_server_url == "https://events.example.com"
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.linear_api_key == "lin_api_test"


def test_defaults_when_no_config(tmp_path):
    cfg = Config.load(tmp_path)

    assert cfg.event_server_url == ""
    assert cfg.slack_bot_token == ""
    assert cfg.linear_api_key == ""


def test_from_file_alias(tmp_path):
    cfg = Config.from_file(tmp_path)
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
