"""Tests for runtime config loading from agent.yaml."""

import os
from pathlib import Path
from textwrap import dedent

import pytest

from bobi.config import (Config, ServiceConfig, find_env_var_refs,
                         find_required_env_vars, load_dotenv)


def test_defaults_when_no_config(tmp_path):
    cfg = Config.load(tmp_path)

    assert cfg.event_server_url == ""
    assert cfg.credential("slack", "bot_token") == ""
    assert cfg.credential("linear", "api_key") == ""


def _write_agent_yaml(tmp_path, body):
    d = tmp_path / "package"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(dedent(body))


def test_parse_channels_helper():
    from bobi.config import _parse_channels
    assert _parse_channels(None) == []
    assert _parse_channels("") == []
    assert _parse_channels(["C1", "C2"]) == ["C1", "C2"]
    assert _parse_channels("C1,C2") == ["C1", "C2"]
    assert _parse_channels(" C1 , C2 ,") == ["C1", "C2"]  # trims + drops empties


def test_service_channels_from_list(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: x
        services:
          - name: slack
            events: true
            channels: [C0AAA, C0BBB]
            credentials:
              bot_token: xoxb-1
    """)
    cfg = Config.load(tmp_path)
    slack = next(s for s in cfg.services if s.name == "slack")
    assert slack.channels == ["C0AAA", "C0BBB"]


def test_service_channels_from_env_csv(tmp_path):
    os.environ["SLACK_CHANNELS"] = "C0AAA,C0BBB"
    try:
        _write_agent_yaml(tmp_path, """
            entry_point: x
            services:
              - name: slack
                events: true
                channels: ${SLACK_CHANNELS}
                credentials:
                  bot_token: xoxb-1
        """)
        cfg = Config.load(tmp_path)
        slack = next(s for s in cfg.services if s.name == "slack")
        assert slack.channels == ["C0AAA", "C0BBB"]
    finally:
        del os.environ["SLACK_CHANNELS"]


# --- agent.yaml ---


def test_loads_agent_yaml(tmp_path):
    config_dir = tmp_path / "package"
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
          - name: slack
            credentials:
              bot_token: xoxb-agent-yaml
          - name: salesforce

        venn_api_key: venn_test123
    """))

    cfg = Config.load(tmp_path)

    assert cfg.version == "1.0.0"
    assert cfg.entry_point == "director"
    assert cfg.chat == "slack"
    assert cfg.credential("slack", "bot_token") == "xoxb-agent-yaml"
    assert cfg.venn_api_key == "venn_test123"
    assert len(cfg.services) == 4
    assert cfg.services[0].name == "github"
    assert cfg.services[0].events is True
    assert cfg.services[3].name == "salesforce"
    assert cfg.services[3].events is False


def test_agent_yaml_env_var_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "xoxb-from-env")
    monkeypatch.setenv("TEST_VENN_KEY", "venn_from_env")

    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
          - name: slack
            credentials:
              bot_token: ${TEST_BOT_TOKEN}
        venn_api_key: ${TEST_VENN_KEY}
    """))

    cfg = Config.load(tmp_path)

    assert cfg.credential("slack", "bot_token") == "xoxb-from-env"
    assert cfg.venn_api_key == "venn_from_env"


def test_agent_yaml_missing_env_var_becomes_empty(tmp_path):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: email
        venn_api_key: ${NONEXISTENT_VAR_12345}
    """))

    cfg = Config.load(tmp_path)
    assert cfg.venn_api_key == ""


def test_agent_yaml_optional_env_var_uses_fallback(tmp_path):
    """${VAR:-default} resolves to the fallback when VAR is unset; ${VAR:-}
    resolves to "" (an optional value with no fallback)."""
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        venn_api_key: ${NONEXISTENT_VAR_12345:-venn_fallback}
        event_server: ${NONEXISTENT_EVENT_SERVER_12345:-}
    """))

    cfg = Config.load(tmp_path)
    assert cfg.venn_api_key == "venn_fallback"
    assert cfg.event_server_url == ""


def test_agent_yaml_optional_env_var_prefers_environment(tmp_path, monkeypatch):
    """A SET var wins over its ${VAR:-default} fallback (regression: the raw
    'VAR:-default' token used to be looked up in the environment verbatim, so
    optional refs always resolved to "")."""
    monkeypatch.setenv("TEST_OPT_EVENT_SERVER", "wss://events.example.com")

    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        event_server: ${TEST_OPT_EVENT_SERVER:-}
    """))

    cfg = Config.load(tmp_path)
    assert cfg.event_server_url == "wss://events.example.com"


def test_launch_admission_config_defaults_disabled(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: manager
    """)

    cfg = Config.load(tmp_path)

    assert cfg.launch_admission["enabled"] is False
    assert cfg.launch_admission["max_starting_agents"] == 1
    assert cfg.launch_admission["load_per_cpu_soft_limit"] == 1.5
    assert cfg.launch_admission["load_per_cpu_hard_limit"] == 2.0
    assert cfg.launch_admission["min_memory_available_mb"] == 512
    assert cfg.launch_admission["init_failure_window_seconds"] == 600
    assert cfg.launch_admission["init_failure_backoff_threshold"] == 2


def test_launch_admission_config_parsed_from_yaml(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        launch_admission:
          enabled: true
          max_starting_agents: 2
          load_per_cpu_soft_limit: 1.25
          load_per_cpu_hard_limit: 1.75
          min_memory_available_mb: 1024
          init_failure_window_seconds: 300
          init_failure_backoff_threshold: 3
    """)

    cfg = Config.load(tmp_path)

    assert cfg.launch_admission == {
        "enabled": True,
        "max_starting_agents": 2,
        "load_per_cpu_soft_limit": 1.25,
        "load_per_cpu_hard_limit": 1.75,
        "min_memory_available_mb": 1024,
        "init_failure_window_seconds": 300,
        "init_failure_backoff_threshold": 3,
    }


def test_launch_admission_config_clamps_invalid_values(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        launch_admission:
          enabled: "false"
          max_starting_agents: 0
          load_per_cpu_soft_limit: -1
          load_per_cpu_hard_limit: -2
          min_memory_available_mb: -100
          init_failure_window_seconds: 0
          init_failure_backoff_threshold: 0
    """)

    cfg = Config.load(tmp_path)

    assert cfg.launch_admission["enabled"] is False
    assert cfg.launch_admission["max_starting_agents"] == 1
    assert cfg.launch_admission["load_per_cpu_soft_limit"] == 0.1
    assert cfg.launch_admission["load_per_cpu_hard_limit"] == 0.1
    assert cfg.launch_admission["min_memory_available_mb"] == 0
    assert cfg.launch_admission["init_failure_window_seconds"] == 1
    assert cfg.launch_admission["init_failure_backoff_threshold"] == 1


def test_agent_yaml_services_as_strings(tmp_path):
    config_dir = tmp_path / "package"
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


def test_service_required_defaults_false():
    # Constructed directly: a service is optional unless explicitly marked.
    assert ServiceConfig(name="email").required is False


def test_service_required_parsed_from_yaml(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        services:
          - name: github
            required: true
          - name: email
            events: true
    """)
    cfg = Config.load(tmp_path)
    github = next(s for s in cfg.services if s.name == "github")
    email = next(s for s in cfg.services if s.name == "email")
    assert github.required is True
    assert email.required is False  # default when omitted


def test_service_required_string_form_defaults_false(tmp_path):
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        services:
          - github
    """)
    cfg = Config.load(tmp_path)
    assert cfg.services[0].required is False


def test_agent_yaml_monitors(tmp_path):
    config_dir = tmp_path / "package"
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
    config_dir = tmp_path / "package"
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

    config_dir = tmp_path / "package"
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
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (tmp_path / ".env").write_text("MY_TOKEN=secret123\nOTHER_KEY=abc\n")

    monkeypatch.delenv("MY_TOKEN", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)

    load_dotenv(tmp_path)

    assert os.environ["MY_TOKEN"] == "secret123"
    assert os.environ["OTHER_KEY"] == "abc"

    monkeypatch.delenv("MY_TOKEN")
    monkeypatch.delenv("OTHER_KEY")


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (tmp_path / ".env").write_text("MY_TOKEN=from-dotenv\n")

    monkeypatch.setenv("MY_TOKEN", "from-env")
    load_dotenv(tmp_path)

    assert os.environ["MY_TOKEN"] == "from-env"


def test_load_dotenv_skips_comments_and_blanks(tmp_path, monkeypatch):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (tmp_path / ".env").write_text("# comment\n\nVALID=yes\n")

    monkeypatch.delenv("VALID", raising=False)
    load_dotenv(tmp_path)
    assert os.environ["VALID"] == "yes"
    monkeypatch.delenv("VALID")


def test_load_dotenv_strips_quotes(tmp_path, monkeypatch):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (tmp_path / ".env").write_text("SINGLE='quoted'\nDOUBLE=\"quoted\"\n")

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
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        services:
          - name: slack
            credentials:
              bot_token: ${SLACK_BOT_TOKEN}
        venn_api_key: ${VENN_API_KEY}
    """))

    vars = find_required_env_vars(tmp_path)
    assert "SLACK_BOT_TOKEN" in vars
    assert "VENN_API_KEY" in vars


def test_find_required_env_vars_no_config(tmp_path):
    assert find_required_env_vars(tmp_path) == []


def test_find_env_var_refs_required_vs_optional(tmp_path):
    """Bare ${VAR} refs are required; ${VAR:-default} refs are optional and
    carry their fallback. Names are de-duped, plain (no ':-default' suffix),
    and in order of first appearance."""
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        event_server: ${BOBI_EVENT_SERVER:-}
        model: ${OPTIONAL_MODEL:-sonnet}
        services:
          - name: slack
            credentials:
              bot_token: ${SLACK_BOT_TOKEN}
              extra: ${SLACK_BOT_TOKEN}
    """))

    refs = find_env_var_refs(tmp_path)
    assert [r.name for r in refs] == [
        "BOBI_EVENT_SERVER", "OPTIONAL_MODEL", "SLACK_BOT_TOKEN"]
    by_name = {r.name: r for r in refs}
    assert not by_name["BOBI_EVENT_SERVER"].required
    assert by_name["BOBI_EVENT_SERVER"].default == ""
    assert not by_name["OPTIONAL_MODEL"].required
    assert by_name["OPTIONAL_MODEL"].default == "sonnet"
    assert by_name["SLACK_BOT_TOKEN"].required

    # find_required_env_vars keeps only the bare (required) refs.
    assert find_required_env_vars(tmp_path) == ["SLACK_BOT_TOKEN"]


def test_auto_dispatch_templates_are_not_env_refs(tmp_path):
    _write_agent_yaml(tmp_path, """
        auto_dispatch:
          - workflow: bug-triage
            task: "Triage ${{input.title}} at ${{input.severity}} with ${REAL_SECRET}"
    """)

    refs = find_env_var_refs(tmp_path)

    assert [ref.name for ref in refs] == ["REAL_SECRET"]
    assert find_required_env_vars(tmp_path) == ["REAL_SECRET"]


def test_config_load_preserves_auto_dispatch_template_while_interpolating_env(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("REAL_SECRET", "resolved-secret")
    _write_agent_yaml(tmp_path, """
        auto_dispatch:
          - workflow: bug-triage
            task: "Triage ${{input.title}} with ${REAL_SECRET}"
    """)

    cfg = Config.load(tmp_path)

    assert cfg.auto_dispatch[0]["task"] == (
        "Triage ${{input.title}} with resolved-secret"
    )


def test_config_load_interpolates_env_nested_in_workflow_template(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("REAL_SECRET", "resolved-secret")
    _write_agent_yaml(tmp_path, """
        auto_dispatch:
          - workflow: bug-triage
            task: "${{ ${REAL_SECRET} }}"
    """)

    cfg = Config.load(tmp_path)

    assert cfg.auto_dispatch[0]["task"] == "${{ resolved-secret }}"


def test_dotenv_resolves_in_config(tmp_path, monkeypatch):
    """Full integration: .env values resolve through ${VAR} in agent.yaml."""
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        entry_point: manager
        services:
          - name: slack
            credentials:
              bot_token: ${TEST_DOTENV_TOKEN}
    """))
    (tmp_path / ".env").write_text("TEST_DOTENV_TOKEN=xoxb-from-dotenv\n")

    monkeypatch.delenv("TEST_DOTENV_TOKEN", raising=False)
    load_dotenv(tmp_path)
    cfg = Config.load(tmp_path)
    assert cfg.credential("slack", "bot_token") == "xoxb-from-dotenv"
    monkeypatch.delenv("TEST_DOTENV_TOKEN")


def test_config_load_dotenv_values_do_not_leak_between_projects(tmp_path, monkeypatch):
    monkeypatch.delenv("SHARED_SLACK_TOKEN", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, token in [(first, "xoxb-first"), (second, "xoxb-second")]:
        _write_agent_yaml(root, """
            services:
              - name: slack
                events: true
                credentials:
                  bot_token: ${SHARED_SLACK_TOKEN}
        """)
        (root / ".env").write_text(f"SHARED_SLACK_TOKEN={token}\n")

    assert Config.load(first).credential("slack", "bot_token") == "xoxb-first"
    assert Config.load(second).credential("slack", "bot_token") == "xoxb-second"


def test_config_load_does_not_mutate_process_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CONFIG_ONLY_TOKEN", raising=False)
    _write_agent_yaml(tmp_path, """
        services:
          - name: slack
            credentials:
              bot_token: ${CONFIG_ONLY_TOKEN}
    """)
    (tmp_path / ".env").write_text("CONFIG_ONLY_TOKEN=xoxb-config-only\n")

    cfg = Config.load(tmp_path)

    assert cfg.credential("slack", "bot_token") == "xoxb-config-only"
    assert "CONFIG_ONLY_TOKEN" not in os.environ


def test_venn_services_property(tmp_path):
    config_dir = tmp_path / "package"
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


# --- credential() accessor ---


def test_credential_returns_value(tmp_path):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        services:
          - name: slack
            credentials:
              bot_token: xoxb-123
              app_token: xapp-456
          - name: linear
            credentials:
              api_key: lin_abc
    """))

    cfg = Config.load(tmp_path)
    assert cfg.credential("slack", "bot_token") == "xoxb-123"
    assert cfg.credential("slack", "app_token") == "xapp-456"
    assert cfg.credential("linear", "api_key") == "lin_abc"


def test_credential_missing_service():
    cfg = Config()
    assert cfg.credential("slack", "bot_token") == ""


def test_credential_missing_key(tmp_path):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        services:
          - name: slack
            credentials:
              bot_token: xoxb-123
    """))

    cfg = Config.load(tmp_path)
    assert cfg.credential("slack", "app_token") == ""
    assert cfg.credential("slack", "nonexistent") == ""


def test_unset_optional_slack_app_token_interpolates_to_empty(
    tmp_path, monkeypatch,
):
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        services:
          - name: slack
            credentials:
              bot_token: ${SLACK_BOT_TOKEN}
              app_token: ${SLACK_APP_TOKEN:-}
    """))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-configured")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    cfg = Config.load(tmp_path)

    assert cfg.credential("slack", "app_token") == ""


def test_service_config_credentials_parsed(tmp_path):
    """ServiceConfig.credentials dict is populated from agent.yaml."""
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(dedent("""
        services:
          - name: github
            events: true
          - name: slack
            events: true
            credentials:
              bot_token: xoxb-test
    """))

    cfg = Config.load(tmp_path)
    assert cfg.services[0].credentials == {}
    assert cfg.services[1].credentials == {"bot_token": "xoxb-test"}


# --- requires: parsing ---


def test_requires_parsed(tmp_path):
    """Valid requires: block populates Config.requires."""
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        requires:
          - name: gstack
            why: "skills needed"
            check: "test -e /tmp/gstack"
            fix: "install gstack"
    """)
    cfg = Config.load(tmp_path)
    assert len(cfg.requires) == 1
    assert cfg.requires[0].name == "gstack"
    assert cfg.requires[0].why == "skills needed"
    assert cfg.requires[0].check == "test -e /tmp/gstack"
    assert cfg.requires[0].fix == "install gstack"


def test_requires_empty(tmp_path):
    """Empty or missing requires: yields empty list."""
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        requires: []
    """)
    cfg = Config.load(tmp_path)
    assert cfg.requires == []


def test_requires_missing_key(tmp_path):
    """No requires: key at all yields empty list."""
    _write_agent_yaml(tmp_path, """
        entry_point: manager
    """)
    cfg = Config.load(tmp_path)
    assert cfg.requires == []


def test_requires_skips_invalid_entries(tmp_path):
    """Entries missing name or check are skipped."""
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        requires:
          - name: good
            check: "true"
          - name: no-check
          - check: "no-name"
          - name: also-good
            check: "echo ok"
    """)
    cfg = Config.load(tmp_path)
    assert len(cfg.requires) == 2
    assert cfg.requires[0].name == "good"
    assert cfg.requires[1].name == "also-good"


def test_requires_not_env_interpolated(tmp_path, monkeypatch):
    """check: and fix: strings are NOT interpolated via _interpolate_env."""
    monkeypatch.setenv("HOME", "/home/testuser")
    _write_agent_yaml(tmp_path, """
        entry_point: manager
        requires:
          - name: gstack
            check: "test -e ${HOME}/gstack"
            fix: "cd ${HOME}/gstack && ./setup"
    """)
    cfg = Config.load(tmp_path)
    # The ${HOME} should remain literal (for shell expansion), not resolved
    assert "${HOME}" in cfg.requires[0].check
    assert "${HOME}" in cfg.requires[0].fix


# --- run_requires_checks() shared runner ---


def test_run_requires_checks_all_pass():
    from bobi.config import RequiresEntry, run_requires_checks
    entries = [
        RequiresEntry(name="true-cmd", check="true"),
        RequiresEntry(name="echo-cmd", check="echo ok"),
    ]
    results = run_requires_checks(entries)
    assert len(results) == 2
    for entry, ok, detail in results:
        assert ok is True


def test_run_requires_checks_failure():
    from bobi.config import RequiresEntry, run_requires_checks
    entries = [RequiresEntry(name="fail", check="false")]
    results = run_requires_checks(entries)
    assert len(results) == 1
    entry, ok, detail = results[0]
    assert ok is False


def test_run_requires_checks_timeout():
    from bobi.config import RequiresEntry, run_requires_checks
    entries = [RequiresEntry(name="slow", check="sleep 60")]
    results = run_requires_checks(entries, timeout=0.1)
    entry, ok, detail = results[0]
    assert ok is False
    assert "timed out" in detail


def test_run_requires_checks_command_not_found():
    from bobi.config import RequiresEntry, run_requires_checks
    entries = [RequiresEntry(name="missing", check="nonexistent_cmd_xyz_12345")]
    results = run_requires_checks(entries)
    entry, ok, detail = results[0]
    assert ok is False


def test_run_requires_checks_empty():
    from bobi.config import run_requires_checks
    assert run_requires_checks([]) == []


# --- package-file env-ref scanning (scan_* — the not-yet-installed variant) ---

def test_scan_required_vars_excludes_defaulted(tmp_path):
    from bobi.config import scan_required_vars

    y = tmp_path / "agent.yaml"
    y.write_text("a: ${REQUIRED}\nb: ${OPTIONAL:-x}\nc: literal\n")
    assert scan_required_vars(y) == ["REQUIRED"]


def test_scan_declared_vars_keeps_optional_refs(tmp_path):
    from bobi.config import scan_declared_vars

    y = tmp_path / "agent.yaml"
    y.write_text("a: ${REQUIRED}\nb: ${OPTIONAL:-x}\nc: ${REQUIRED}\n")
    assert scan_declared_vars(y) == ["REQUIRED", "OPTIONAL"]


# --- build-time-only ${VAR} refs ---------------------------------------------
# Regression: a token referenced only by a `build:` step (the image-bake layer)
# was classified a required RUNTIME secret, so `bobi agents install
# --non-interactive` refused to install an agent whose dependency was already
# baked into the image. It took eng-team's fleet roll down on 2026-07-31: the
# deploy side deliberately withholds build secrets from the runtime env-file
# (the deploy plugin's BUILD_SECRET_NAMES, "must never be required as,
# backfilled into, or persisted as runtime secrets"), and this side demanded one.

def test_build_only_ref_is_not_required_to_run(tmp_path):
    """A ${VAR} used only by a build step is build_only, so it never gates a
    runtime. This is eng-team's exact shape: a private clone in build.run_root."""
    _write_agent_yaml(tmp_path, """
        name: eng-team
        build:
          run_root:
            - git clone https://x-access-token:${MODA_SKILLS_TOKEN}@github.com/acme/skills /opt/skills
    """)

    by_name = {r.name: r for r in find_env_var_refs(tmp_path)}
    assert by_name["MODA_SKILLS_TOKEN"].build_only
    # Still a bare ref, so still "required" in the ${VAR} sense...
    assert by_name["MODA_SKILLS_TOKEN"].required
    # ...but not required to RUN, which is what the install gate reads.
    assert find_required_env_vars(tmp_path) == []


def test_runtime_ref_is_still_required(tmp_path):
    """The guard against over-correcting. An exclusion broad enough to swallow
    ordinary runtime credentials would pass the test above and fail this one."""
    _write_agent_yaml(tmp_path, """
        services:
          - name: slack
            credentials:
              bot_token: ${SLACK_BOT_TOKEN}
        build:
          run_root:
            - git clone https://x-access-token:${MODA_SKILLS_TOKEN}@github.com/acme/skills /opt/skills
    """)

    assert find_required_env_vars(tmp_path) == ["SLACK_BOT_TOKEN"]


def test_a_name_used_both_places_stays_required(tmp_path):
    """Build use does not launder a runtime requirement: the runtime use is
    real, so the name is required even though `build:` also mentions it."""
    _write_agent_yaml(tmp_path, """
        services:
          - name: gh
            credentials:
              token: ${GH_TOKEN}
        build:
          run_root:
            - git clone https://x-access-token:${GH_TOKEN}@github.com/acme/x /opt/x
    """)

    by_name = {r.name: r for r in find_env_var_refs(tmp_path)}
    assert not by_name["GH_TOKEN"].build_only
    assert find_required_env_vars(tmp_path) == ["GH_TOKEN"]


def test_unparseable_agent_yaml_keeps_every_ref_required(tmp_path):
    """Fail SAFE. If the document will not parse we cannot prove a ref is
    build-only, so nothing is downgraded and the previous behaviour stands."""
    config_dir = tmp_path / "package"
    config_dir.mkdir()
    (config_dir / "agent.yaml").write_text(
        "build:\n  run_root:\n    - clone ${BUILD_TOKEN}\n  bad: [unclosed\n")

    by_name = {r.name: r for r in find_env_var_refs(tmp_path)}
    assert not by_name["BUILD_TOKEN"].build_only
    assert find_required_env_vars(tmp_path) == ["BUILD_TOKEN"]


def test_build_only_applies_to_every_build_step_kind(tmp_path):
    """apt/npm/run/run_root are all image-bake steps, not runtime reads."""
    _write_agent_yaml(tmp_path, """
        build:
          apt: ["${APT_VAR}"]
          npm: ["${NPM_VAR}"]
          run: ["echo ${RUN_VAR}"]
          run_root: ["echo ${RUN_ROOT_VAR}"]
    """)

    assert find_required_env_vars(tmp_path) == []
    assert all(r.build_only for r in find_env_var_refs(tmp_path))


# D085 — a key present with an empty value is YAML null, not a missing key, so
# `raw.get(key, default)` returns None and the default never applies. Every
# start/status/dispatch path goes through Config.load, so one commented-out
# value in agent.yaml took the whole team down with a traceback.

@pytest.mark.parametrize("key", ["event_server", "spend_cap", "services",
                                 "requires"])
def test_null_valued_key_falls_back_to_the_default(tmp_path, key):
    _write_agent_yaml(tmp_path, f"""
        agent: demo
        {key}:
    """)

    cfg = Config.load(tmp_path)

    assert cfg.agent == "demo"
    assert cfg.services == []
    assert cfg.requires == []
    assert cfg.spend_cap == 0
    assert cfg.event_server_url == ""


def test_every_top_level_key_survives_a_null_value(tmp_path):
    """No key may turn Config.load into a traceback when its value is empty.

    Enumerated rather than spot-checked: D085 was filed against two keys and
    four were actually broken.
    """
    keys = ["agent", "version", "entry_point", "chat", "brain", "roles",
            "services", "requires", "monitors", "workflows", "auto_dispatch",
            "event_server", "spend_cap", "mcp_servers", "channels",
            "venn_api_key", "build", "repos", "tools", "skills"]
    broken = []
    for key in keys:
        _write_agent_yaml(tmp_path, f"agent: demo\n{key}:\n")
        try:
            Config.load(tmp_path)
        except Exception as e:            # noqa: BLE001 - reporting every one
            broken.append(f"{key}: {type(e).__name__}: {e}")
    assert not broken, "null value crashed Config.load for:\n  " + \
        "\n  ".join(broken)
