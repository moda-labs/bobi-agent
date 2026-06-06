"""Tests for config loading — machine → project resolution."""

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from modastack.config import Config, load_deployment_state, save_deployment_state


def test_project_config_loads(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        agent:
          max_parallel: 3
        context:
          github_org: myorg
    """))

    config = Config.load(tmp_path)

    assert config.path == tmp_path
    assert config.max_parallel == 3
    assert config.context["github_org"] == "myorg"


def test_project_config_linear(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        linear:
          team: MOD
          project: Baohua
    """))

    config = Config.load(tmp_path)
    assert config.linear_team == "MOD"
    assert config.linear_project == "Baohua"


def test_project_config_defaults(tmp_path):
    config = Config.load(tmp_path)
    assert config.max_parallel == 2
    assert config.linear_team == ""
    assert config.context == {}


def test_project_config_event_server(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        event_server:
          url: https://modastack-events.example.com
    """))

    config = Config.load(tmp_path)
    assert config.event_server_url == "https://modastack-events.example.com"


def test_machine_config_provides_defaults(tmp_path):
    machine_yaml = tmp_path / "machine_config.yaml"
    machine_yaml.write_text(dedent("""
        slack:
          bot_token: xoxb-machine-token
        event_server:
          url: https://events.example.com
    """))

    project_dir = tmp_path / "project"
    config_dir = project_dir / ".modastack"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("{}")

    with patch("modastack.config._machine_config_path", return_value=machine_yaml):
        config = Config.load(project_dir)

    assert config.slack_bot_token == "xoxb-machine-token"
    assert config.event_server_url == "https://events.example.com"


def test_project_overrides_machine(tmp_path):
    machine_yaml = tmp_path / "machine_config.yaml"
    machine_yaml.write_text(dedent("""
        event_server:
          url: https://machine-default.example.com
    """))

    project_dir = tmp_path / "project"
    config_dir = project_dir / ".modastack"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(dedent("""
        event_server:
          url: https://project-specific.example.com
    """))

    with patch("modastack.config._machine_config_path", return_value=machine_yaml):
        config = Config.load(project_dir)

    assert config.event_server_url == "https://project-specific.example.com"


def test_from_file_alias_works(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}")

    config = Config.from_file(tmp_path)
    assert config.path == tmp_path


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
