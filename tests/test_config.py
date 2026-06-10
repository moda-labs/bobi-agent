"""Tests for per-project config loading from agent.yaml."""

import os
from pathlib import Path
from textwrap import dedent

from modastack.config import Config, ServiceConfig, load_deployment_state, save_deployment_state, load_dotenv, find_required_env_vars


def test_defaults_when_no_config(tmp_path):
    cfg = Config.load(tmp_path)

    assert cfg.event_server_url == ""
    assert cfg.slack_bot_token == ""
    assert cfg.linear_api_key == ""


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


# --- agent.yaml ---


def test_loads_agent_yaml(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        version: "1.0.0"
        entry_point: director
        chat: slack

        services:
          - name: github
            events: true
          - name: email
            events: true
          - name: salesforce

        slack:
          bot_token: xoxb-agent-yaml

        venn_api_key: venn_test123
    """))

    cfg = Config.load(tmp_path)

    assert cfg.version == "1.0.0"
    assert cfg.entry_point == "director"
    assert cfg.chat == "slack"
    assert cfg.slack_bot_token == "xoxb-agent-yaml"
    assert cfg.venn_api_key == "venn_test123"
    assert len(cfg.services) == 3
    assert cfg.services[0].name == "github"
    assert cfg.services[0].events is True
    assert cfg.services[2].name == "salesforce"
    assert cfg.services[2].events is False


def test_agent_yaml_env_var_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "xoxb-from-env")
    monkeypatch.setenv("TEST_VENN_KEY", "venn_from_env")

    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
        slack:
          bot_token: ${TEST_BOT_TOKEN}
        venn_api_key: ${TEST_VENN_KEY}
    """))

    cfg = Config.load(tmp_path)

    assert cfg.slack_bot_token == "xoxb-from-env"
    assert cfg.venn_api_key == "venn_from_env"


def test_agent_yaml_missing_env_var_becomes_empty(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
        venn_api_key: ${NONEXISTENT_VAR_12345}
    """))

    cfg = Config.load(tmp_path)
    assert cfg.venn_api_key == ""


def test_agent_yaml_services_as_strings(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - github
          - email
    """))

    cfg = Config.load(tmp_path)
    assert len(cfg.services) == 2
    assert cfg.services[0].name == "github"
    assert cfg.services[0].events is False


def test_agent_yaml_monitors(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
            events: true
        monitors:
          - name: new-emails
            command: venn exec gmail list_messages '{}'
            interval: 5m
            event: email/received
    """))

    cfg = Config.load(tmp_path)
    assert len(cfg.monitors) == 1
    assert cfg.monitors[0]["name"] == "new-emails"
    assert cfg.monitors[0]["command"].startswith("venn exec")


def test_agent_yaml_mcp_servers(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
        mcp_servers:
          internal-crm:
            type: http
            url: https://crm.internal/mcp
            headers:
              Authorization: Bearer test-token
          local-tools:
            type: stdio
            command: node
            args:
              - tools/server.js
    """))

    cfg = Config.load(tmp_path)
    assert len(cfg.mcp_servers) == 2
    assert cfg.mcp_servers["internal-crm"]["type"] == "http"
    assert cfg.mcp_servers["internal-crm"]["url"] == "https://crm.internal/mcp"
    assert cfg.mcp_servers["local-tools"]["type"] == "stdio"
    assert cfg.mcp_servers["local-tools"]["command"] == "node"


def test_mcp_servers_env_var_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_TOKEN", "secret-123")

    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        mcp_servers:
          crm:
            type: http
            url: https://crm.internal/mcp
            headers:
              Authorization: Bearer ${CRM_TOKEN}
    """))

    cfg = Config.load(tmp_path)
    assert cfg.mcp_servers["crm"]["headers"]["Authorization"] == "Bearer secret-123"


# --- .env loading ---


def test_load_dotenv(tmp_path, monkeypatch):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / ".env").write_text("MY_TOKEN=secret123\nOTHER_KEY=abc\n")

    monkeypatch.delenv("MY_TOKEN", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)

    load_dotenv(tmp_path)

    assert os.environ["MY_TOKEN"] == "secret123"
    assert os.environ["OTHER_KEY"] == "abc"

    monkeypatch.delenv("MY_TOKEN")
    monkeypatch.delenv("OTHER_KEY")


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / ".env").write_text("MY_TOKEN=from-dotenv\n")

    monkeypatch.setenv("MY_TOKEN", "from-env")
    load_dotenv(tmp_path)

    assert os.environ["MY_TOKEN"] == "from-env"


def test_load_dotenv_skips_comments_and_blanks(tmp_path, monkeypatch):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / ".env").write_text("# comment\n\nVALID=yes\n")

    monkeypatch.delenv("VALID", raising=False)
    load_dotenv(tmp_path)
    assert os.environ["VALID"] == "yes"
    monkeypatch.delenv("VALID")


def test_load_dotenv_strips_quotes(tmp_path, monkeypatch):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / ".env").write_text("SINGLE='quoted'\nDOUBLE=\"quoted\"\n")

    monkeypatch.delenv("SINGLE", raising=False)
    monkeypatch.delenv("DOUBLE", raising=False)
    load_dotenv(tmp_path)
    assert os.environ["SINGLE"] == "quoted"
    assert os.environ["DOUBLE"] == "quoted"
    monkeypatch.delenv("SINGLE")
    monkeypatch.delenv("DOUBLE")


def test_load_dotenv_missing_file(tmp_path):
    load_dotenv(tmp_path)


def test_find_required_env_vars(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        slack:
          bot_token: ${SLACK_BOT_TOKEN}
        venn_api_key: ${VENN_API_KEY}
    """))

    vars = find_required_env_vars(tmp_path)
    assert "SLACK_BOT_TOKEN" in vars
    assert "VENN_API_KEY" in vars


def test_find_required_env_vars_no_config(tmp_path):
    assert find_required_env_vars(tmp_path) == []


def test_dotenv_resolves_in_config(tmp_path, monkeypatch):
    """Full integration: .env values resolve through ${VAR} in agent.yaml."""
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        slack:
          bot_token: ${TEST_DOTENV_TOKEN}
    """))
    (config_dir / ".env").write_text("TEST_DOTENV_TOKEN=xoxb-from-dotenv\n")

    monkeypatch.delenv("TEST_DOTENV_TOKEN", raising=False)
    load_dotenv(tmp_path)
    cfg = Config.load(tmp_path)
    assert cfg.slack_bot_token == "xoxb-from-dotenv"
    monkeypatch.delenv("TEST_DOTENV_TOKEN")


def test_venn_services_property(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: github
          - name: slack
          - name: email
          - name: salesforce
    """))

    cfg = Config.load(tmp_path)
    venn = cfg.venn_services
    assert len(venn) == 2
    assert {s.name for s in venn} == {"email", "salesforce"}
