"""Integration tests for unified agent.yaml config format.

Verifies that the config system correctly loads agent.yaml with env var
interpolation, service declarations, and command monitors.
"""

import os
import subprocess
import sys
import textwrap

import pytest
import yaml


class TestAgentYamlConfig:
    """Config loading from agent.yaml in a real project directory."""

    def test_loads_unified_agent_yaml(self, tmp_path):
        """agent.yaml with entry_point + services is recognized as unified format."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
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
              bot_token: xoxb-test-token
            venn_api_key: venn_test
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.entry_point == "director"
        assert cfg.chat == "slack"
        assert cfg.venn_api_key == "venn_test"
        assert cfg.slack_bot_token == "xoxb-test-token"
        assert len(cfg.services) == 3
        assert cfg.services[0].name == "github"
        assert cfg.services[0].events is True
        assert cfg.services[2].name == "salesforce"
        assert cfg.services[2].events is False

    def test_env_var_interpolation_end_to_end(self, tmp_path, monkeypatch):
        """${ENV_VAR} in agent.yaml is resolved from the environment."""
        monkeypatch.setenv("INTEG_SLACK_TOKEN", "xoxb-from-env-123")
        monkeypatch.setenv("INTEG_VENN_KEY", "venn_env_456")

        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            services:
              - name: email
            slack:
              bot_token: ${INTEG_SLACK_TOKEN}
            venn_api_key: ${INTEG_VENN_KEY}
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.slack_bot_token == "xoxb-from-env-123"
        assert cfg.venn_api_key == "venn_env_456"

    def test_no_agent_yaml_returns_empty_config(self, tmp_path):
        """Missing agent.yaml returns a default empty Config."""
        from modastack.config import Config
        cfg = Config.load(tmp_path)

        assert cfg.entry_point == ""
        assert cfg.services == []
        assert cfg.slack_bot_token == ""

    def test_venn_services_vs_native(self, tmp_path):
        """Services property correctly separates native from Venn-backed."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: director
            services:
              - name: github
              - name: slack
              - name: linear
              - name: email
              - name: salesforce
              - name: calendar
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)

        native_names = {s.name for s in cfg.services if s.name in cfg.native_services}
        venn_names = {s.name for s in cfg.venn_services}

        assert native_names == {"github", "slack", "linear"}
        assert venn_names == {"email", "salesforce", "calendar"}

    def test_monitors_in_agent_yaml(self, tmp_path):
        """Monitor definitions in agent.yaml are parsed correctly."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(textwrap.dedent("""\
            entry_point: manager
            services:
              - name: email
                events: true
            monitors:
              - name: new-emails
                command: venn exec gmail list_messages '{}'
                interval: 5m
                event: email/received
              - name: sf-updates
                command: venn exec salesforce query '{}'
                interval: 15m
                event: salesforce/updated
        """))

        from modastack.config import Config
        cfg = Config.load(tmp_path)

        assert len(cfg.monitors) == 2
        assert cfg.monitors[0]["name"] == "new-emails"
        assert "venn exec" in cfg.monitors[0]["command"]
        assert cfg.monitors[1]["interval"] == "15m"


class TestCommandMonitorIntegration:
    """Command monitors running real shell commands."""

    def test_command_monitor_end_to_end(self, tmp_path):
        """Full scheduler tick with a command monitor producing events."""
        from datetime import datetime, timezone
        from modastack.monitors.schema import Monitor
        from modastack.monitors.scheduler import MonitorScheduler

        injected = []
        m = Monitor(
            name="test-cmd",
            command='echo \'[{"id": "item1", "value": "hello"}, {"id": "item2", "value": "world"}]\'',
            event="test/items",
            interval="1m",
        )

        class FakeRegistry:
            def effective_monitors(self):
                return [m]
            def projects_for(self, _m):
                return []

        sched = MonitorScheduler(
            inject_event=injected.append,
            state_path=tmp_path / "state.json",
            now=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            registry_loader=lambda: FakeRegistry(),
            spawn_check=lambda _m, _c: None,
        )

        sched.tick()

        assert len(injected) == 2
        assert injected[0]["source"] == "test"
        assert injected[0]["type"] == "items"
        assert injected[0]["data"]["value"] == "hello"
        assert injected[1]["data"]["value"] == "world"

        # Second tick — same data, no new events (dedup)
        injected.clear()
        sched.tick()
        assert len(injected) == 0

    def test_command_monitor_with_changing_data(self, tmp_path):
        """Monitor fires new events when data changes between ticks."""
        from datetime import datetime, timedelta, timezone
        from modastack.monitors.schema import Monitor
        from modastack.monitors.scheduler import MonitorScheduler

        injected = []
        script = tmp_path / "data.sh"
        data_file = tmp_path / "data.json"

        # First run: one item
        data_file.write_text('[{"id": "a"}]')
        script.write_text(f"#!/bin/sh\ncat {data_file}")
        script.chmod(0o755)

        m = Monitor(
            name="changing",
            command=str(script),
            event="test/change",
            interval="1m",
        )

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        class FakeRegistry:
            def effective_monitors(self):
                return [m]
            def projects_for(self, _m):
                return []

        sched = MonitorScheduler(
            inject_event=injected.append,
            state_path=tmp_path / "state.json",
            now=lambda: t,
            registry_loader=lambda: FakeRegistry(),
            spawn_check=lambda _m, _c: None,
        )

        sched.tick()
        assert len(injected) == 1

        # Second run: add a new item
        data_file.write_text('[{"id": "a"}, {"id": "b"}]')
        t = t + timedelta(minutes=2)
        sched._now = lambda: t

        sched.tick()
        assert len(injected) == 2  # only "b" is new
        assert injected[1]["data"]["id"] == "b"
