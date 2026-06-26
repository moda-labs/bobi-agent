"""Integration tests for config resolution chain.

Exercises the full config loading path: .env → agent.yaml interpolation,
deployment state round-trips, channel parsing from env vars, credential
lookup, connections, and requires checks — all against a real filesystem
project layout.
"""

import os
import textwrap

import pytest
import yaml


class TestDotenvResolution:
    """dotenv loading feeds into agent.yaml ${VAR} interpolation."""

    def test_dotenv_feeds_agent_yaml(self, tmp_path, monkeypatch):
        """Values in .env resolve ${VAR} references in agent.yaml."""
        # Clear any pre-existing env vars that would override .env
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)

        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()
        (config_dir / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-dotenv-token\n"
            "LINEAR_API_KEY=lin_key_from_dotenv\n"
        )
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            services:
              - name: slack
                credentials:
                  bot_token: ${SLACK_BOT_TOKEN}
              - name: linear
                credentials:
                  api_key: ${LINEAR_API_KEY}
        """))

        from bobi.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.credential("slack", "bot_token") == "xoxb-dotenv-token"
        assert cfg.credential("linear", "api_key") == "lin_key_from_dotenv"

    def test_env_overrides_dotenv(self, tmp_path, monkeypatch):
        """Real environment variables take precedence over .env values."""
        monkeypatch.setenv("MY_TOKEN", "from-real-env")

        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()
        (config_dir / ".env").write_text("MY_TOKEN=from-dotenv\n")
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            venn_api_key: ${MY_TOKEN}
        """))

        from bobi.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.venn_api_key == "from-real-env"

    def test_missing_env_var_resolves_empty(self, tmp_path):
        """An unset ${VAR} resolves to empty string, not a crash."""
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            venn_api_key: ${NONEXISTENT_VAR_12345}
        """))

        from bobi.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.venn_api_key == ""


class TestDeploymentState:
    """Deployment state round-trip: save → load → per-session isolation."""

    def test_roundtrip(self, tmp_path):
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()

        from bobi.config import save_deployment_state, load_deployment_state

        save_deployment_state(tmp_path, "sess-a", "deploy-1", "key-1")
        state = load_deployment_state(tmp_path, "sess-a")

        assert state["deployment_id"] == "deploy-1"
        assert state["api_key"] == "key-1"

    def test_per_session_isolation(self, tmp_path):
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()

        from bobi.config import save_deployment_state, load_deployment_state

        save_deployment_state(tmp_path, "sess-a", "deploy-a", "key-a")
        save_deployment_state(tmp_path, "sess-b", "deploy-b", "key-b")

        assert load_deployment_state(tmp_path, "sess-a")["deployment_id"] == "deploy-a"
        assert load_deployment_state(tmp_path, "sess-b")["deployment_id"] == "deploy-b"

    def test_missing_returns_empty(self, tmp_path):
        from bobi.config import load_deployment_state
        assert load_deployment_state(tmp_path, "nonexistent") == {}


class TestChannelParsing:
    """Channel parsing from env var CSV strings."""

    def test_csv_channels_from_env(self, tmp_path, monkeypatch):
        """Comma-separated channel string from ${VAR} is parsed to list."""
        monkeypatch.setenv("MY_CHANNELS", "C001,C002,C003")

        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            services:
              - name: slack
                events: true
                channels: ${MY_CHANNELS}
        """))

        from bobi.config import Config
        cfg = Config.load(tmp_path)

        slack = [s for s in cfg.services if s.name == "slack"][0]
        assert slack.channels == ["C001", "C002", "C003"]

    def test_list_channels_literal(self, tmp_path):
        """YAML list channels are preserved as-is."""
        config_dir = tmp_path / ".bobi"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            services:
              - name: slack
                events: true
                channels:
                  - C100
                  - C200
        """))

        from bobi.config import Config
        cfg = Config.load(tmp_path)

        slack = [s for s in cfg.services if s.name == "slack"][0]
        assert slack.channels == ["C100", "C200"]


class TestRequiresChecks:
    """Health-check commands declared in requires: block."""

    def test_passing_check(self, tmp_path):
        from bobi.config import RequiresEntry, run_requires_checks

        entry = RequiresEntry(name="echo", check="echo ok", why="test", fix="n/a")
        results = run_requires_checks([entry], timeout=5)

        assert len(results) == 1
        assert results[0][1] is True  # passed

    def test_failing_check(self, tmp_path):
        from bobi.config import RequiresEntry, run_requires_checks

        entry = RequiresEntry(name="fail", check="exit 1", why="test", fix="n/a")
        results = run_requires_checks([entry], timeout=5)

        assert len(results) == 1
        assert results[0][1] is False  # failed
