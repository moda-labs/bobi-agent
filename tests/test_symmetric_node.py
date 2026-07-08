"""Tests for symmetric node architecture — events package, subscriptions, drain, agent config."""

import json
import threading
import time
from pathlib import Path
from queue import SimpleQueue
from textwrap import dedent
from unittest.mock import patch, MagicMock

import pytest
import yaml

from bobi import paths
from bobi.events.subscriptions import discover_subscriptions
from bobi.events.drain import drain_loop, DRAIN_INTERVAL
from bobi.events.client import format_event_for_manager, event_queue


# ---------------------------------------------------------------------------
# bobi.events.subscriptions
# ---------------------------------------------------------------------------


class TestBuildSubscriptions:

    def test_reads_agent_yaml(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "subscribe:\n  - github:org/repo\n  - slack:T123\n  - linear:MOD\n"
        )
        subs = discover_subscriptions(tmp_path)
        assert "github:org/repo" in subs
        assert "slack:T123" in subs
        assert "linear:MOD" in subs

    def test_interpolates_agent_yaml_subscribe_entries(self, tmp_path, monkeypatch):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.env_path(tmp_path).write_text("ALERT_TOPIC=alert/firing\n")
        monkeypatch.setenv("ORG_REPO_TOPIC", "github:org/repo")
        monkeypatch.delenv("FALLBACK_TOPIC", raising=False)
        paths.agent_yaml_path(tmp_path).write_text(
            "subscribe:\n"
            "  - ${ORG_REPO_TOPIC}\n"
            "  - ${ALERT_TOPIC}\n"
            "  - ${FALLBACK_TOPIC:-linear:MOD}\n"
            "  - slack:T123\n"
        )

        subs = discover_subscriptions(tmp_path)

        assert subs == [
            "github:org/repo",
            "alert/firing",
            "linear:MOD",
            "slack:T123",
        ]

    def test_interpolated_subscriptions_do_not_leak_between_projects(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("SHARED_TOPIC", raising=False)
        first = tmp_path / "first"
        second = tmp_path / "second"
        for root, topic in [(first, "github:first/repo"), (second, "github:second/repo")]:
            paths.package_dir(root).mkdir(parents=True)
            paths.env_path(root).write_text(f"SHARED_TOPIC={topic}\n")
            paths.agent_yaml_path(root).write_text("subscribe:\n  - ${SHARED_TOPIC}\n")

        assert discover_subscriptions(first) == ["github:first/repo"]
        assert discover_subscriptions(second) == ["github:second/repo"]

    def test_fallback_dotenv_does_not_satisfy_later_explicit_subscribe(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("FALLBACK_LEAK_TOPIC", raising=False)
        first = tmp_path / "first"
        second = tmp_path / "second"
        paths.package_dir(first).mkdir(parents=True)
        paths.env_path(first).write_text("FALLBACK_LEAK_TOPIC=github:first/repo\n")
        paths.agent_yaml_path(first).write_text("agent: first\n")
        paths.package_dir(second).mkdir(parents=True)
        paths.agent_yaml_path(second).write_text(
            "subscribe:\n  - ${FALLBACK_LEAK_TOPIC}\n"
        )

        assert discover_subscriptions(first) == ["first"]
        assert discover_subscriptions(second) == ["second"]

    def test_ignores_empty_or_invalid_explicit_subscriptions(self, tmp_path, monkeypatch):
        paths.package_dir(tmp_path).mkdir(parents=True)
        monkeypatch.delenv("MISSING_TOPIC", raising=False)
        paths.agent_yaml_path(tmp_path).write_text(
            "subscribe:\n"
            "  - ${MISSING_TOPIC}\n"
            "  - ${OPTIONAL_TOPIC:-}\n"
            "  - 123\n"
        )

        subs = discover_subscriptions(tmp_path)

        assert subs == [tmp_path.name]

    def test_accepts_scalar_interpolated_subscribe_entry(self, tmp_path, monkeypatch):
        paths.package_dir(tmp_path).mkdir(parents=True)
        monkeypatch.setenv("SCALAR_TOPIC", "slack:T123")
        paths.agent_yaml_path(tmp_path).write_text("subscribe: ${SCALAR_TOPIC}\n")

        subs = discover_subscriptions(tmp_path)

        assert subs == ["slack:T123"]

    def test_fallback_to_dir_name(self, tmp_path):
        subs = discover_subscriptions(tmp_path)
        assert tmp_path.name in subs


# ---------------------------------------------------------------------------
# bobi.events.drain
# ---------------------------------------------------------------------------


class TestDrainLoop:

    def test_batches_and_delivers(self):
        from bobi.inbox import register_local_inbox, unregister_local_inbox

        queue = SimpleQueue()
        pushed = []

        class _CaptureInbox:
            def push(self, msg, priority=False):
                pushed.append(msg)

        def mock_formatter(event):
            return f"formatted:{event['type']}"

        register_local_inbox("test-session", _CaptureInbox())
        try:
            thread = threading.Thread(
                target=drain_loop,
                args=("test-session", queue, mock_formatter),
                daemon=True,
            )
            thread.start()

            queue.put({"type": "task.opened", "source": "github"})
            queue.put({"type": "pr.opened", "source": "github"})

            time.sleep(DRAIN_INTERVAL + 1)

            assert pushed
            assert "formatted:task.opened" in pushed[0].text
        finally:
            unregister_local_inbox("test-session")

    def test_chat_events_delivered_separately(self):
        """Chat-delivery events (e.g. Slack) are batched separately from bulk."""
        from bobi.inbox import register_local_inbox, unregister_local_inbox

        queue = SimpleQueue()
        pushed = []

        class _CaptureInbox:
            def push(self, msg, priority=False):
                pushed.append(msg)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            thread = threading.Thread(
                target=drain_loop,
                args=("test-session", queue, format_event_for_manager),
                daemon=True,
            )
            thread.start()

            queue.put({"type": "task.opened", "source": "github",
                       "delivery": "bulk",
                       "data": {"issue_id": "1"}})
            queue.put({"type": "slack.dm", "source": "slack",
                       "delivery": "chat",
                       "data": {"text": "hi"}})

            time.sleep(DRAIN_INTERVAL + 1)

            # Bulk group and chat group are pushed separately.
            assert len(pushed) >= 2
        finally:
            unregister_local_inbox("test-session")


# ---------------------------------------------------------------------------
# canonical imports
# ---------------------------------------------------------------------------


class TestCanonicalImports:

    def test_direct_imports_work(self):
        from bobi.events.client import (
            EventServerClient,
            event_queue as eq,
            format_event_for_manager as fmt,
        )
        assert EventServerClient is not None
        assert eq is event_queue
        assert fmt is format_event_for_manager

    def test_server_imports_work(self):
        from bobi.events.server import ensure_running, register
        assert callable(ensure_running)
        assert callable(register)

    def test_discover_subscriptions_direct(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("subscribe:\n  - slack:T999\n")

        from bobi.events.subscriptions import discover_subscriptions
        subs = discover_subscriptions(tmp_path)
        assert "slack:T999" in subs


# ---------------------------------------------------------------------------
# agent config loading (Phase 3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# prompt resolver (Phase 4)
# ---------------------------------------------------------------------------


class TestPromptResolver:

    def test_resolve_agent_prompt_loads_base_and_role(self, bobi_install):
        from bobi.prompts.resolver import resolve_agent_prompt
        mi = bobi_install
        prompt = resolve_agent_prompt("director", mi.repo_path, agent_name=mi.agent_name)
        assert "Bobi Agent" in prompt
        assert "Engineering Director" in prompt

    def test_resolve_agent_prompt_includes_project_override(self, bobi_install):
        mi = bobi_install
        role_dir = paths.roles_dir(mi.repo_path) / "director"
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "ROLE.md").write_text("Custom policy: always review PRs.")
        from bobi.prompts.resolver import resolve_agent_prompt
        prompt = resolve_agent_prompt("director", mi.repo_path, agent_name=mi.agent_name)
        assert "Custom policy: always review PRs." in prompt
        assert "Engineering Director" not in prompt

    def test_resolve_agent_prompt_engineer(self, bobi_install):
        from bobi.prompts.resolver import resolve_agent_prompt
        mi = bobi_install
        prompt = resolve_agent_prompt("engineer", mi.repo_path, agent_name=mi.agent_name)
        assert "Bobi Agent" in prompt
        assert "staff engineer" in prompt

    def test_build_startup_prompt_includes_workflows(self, bobi_install):
        from bobi.prompts.resolver import build_startup_prompt
        mi = bobi_install
        prompt = build_startup_prompt("director", mi.repo_path, agent_name=mi.agent_name)
        assert "Available workflows" in prompt

    def test_list_workflows_returns_string(self, bobi_install):
        from bobi.prompts.resolver import list_workflows
        mi = bobi_install
        result = list_workflows(mi.repo_path, agent_name=mi.agent_name)
        assert isinstance(result, str)

    def test_discover_roles_finds_director_and_engineer(self, bobi_install):
        from bobi.prompts.resolver import discover_roles
        mi = bobi_install
        roles = discover_roles(project_path=mi.repo_path, agent_name=mi.agent_name)
        names = [r["name"] for r in roles]
        assert "director" in names
        assert "engineer" in names

    def test_discover_roles_scans_all_packs_without_agent_name(self, bobi_install):
        from bobi.prompts.resolver import discover_roles
        mi = bobi_install
        roles = discover_roles(project_path=mi.repo_path)
        names = [r["name"] for r in roles]
        assert "director" in names
        assert "engineer" in names


class TestAgentConfig:

    def test_config_load_from_default_path(self, tmp_path):
        from bobi.config import Config
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(dedent("""
            agent: test-agent
            entry_point: manager
            services:
              - name: github
                events: true
        """))
        cfg = Config.load(tmp_path)
        assert cfg.agent == "test-agent"
        assert cfg.entry_point == "manager"
        assert cfg.services[0].name == "github"

    def test_config_load_missing_returns_defaults(self, tmp_path):
        from bobi.config import Config
        cfg = Config.load(tmp_path)
        assert cfg.agent == ""
        assert cfg.entry_point == ""

    def test_config_load_empty_file(self, tmp_path):
        from bobi.config import Config
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("")
        cfg = Config.load(tmp_path)
        assert cfg.agent == ""


# ---------------------------------------------------------------------------
# --subscribe on agent launch (Phase 2)
# ---------------------------------------------------------------------------


class TestSubscribeFlag:
    def test_subscribe_implies_persistent(self, bobi_install):
        from click.testing import CliRunner
        from bobi.cli import main

        with patch("bobi.subagent.launch_agent") as mock_launch, \
             patch("bobi.prompts.resolver.validate_role", return_value=True):
            mock_launch.return_value = "test-session"

            runner = CliRunner()
            result = runner.invoke(main, [
                "agent", bobi_install.agent_name, "subagents", "launch",
                "-w", "adhoc",
                "--role", "manager",
                "--task", "watch events",
                "--subscribe", "moda-labs/bobi",
            ])

            assert result.exit_code == 0
            call_kwargs = mock_launch.call_args[1]
            assert call_kwargs["persistent"] is True
            assert "moda-labs/bobi" in call_kwargs["subscribe"]

    def test_subscribe_multiple_topics(self, bobi_install):
        from click.testing import CliRunner
        from bobi.cli import main

        with patch("bobi.subagent.launch_agent") as mock_launch, \
             patch("bobi.prompts.resolver.validate_role", return_value=True):
            mock_launch.return_value = "test-session"

            runner = CliRunner()
            result = runner.invoke(main, [
                "agent", bobi_install.agent_name, "subagents", "launch",
                "-w", "adhoc",
                "--role", "manager",
                "--task", "watch",
                "--subscribe", "org/repo",
                "--subscribe", "slack:T123",
            ])

            assert result.exit_code == 0
            call_kwargs = mock_launch.call_args[1]
            assert call_kwargs["subscribe"] == ["org/repo", "slack:T123"]

    def test_launch_agent_passes_subscribe_to_args(self, bobi_install):
        from bobi.subagent import launch_agent

        with patch("bobi.subagent._launch_detached", return_value=12345) as mock_det, \
             patch("bobi.subagent.get_registry") as mock_reg:
            mock_reg.return_value = MagicMock()
            mock_reg.return_value.get.return_value = None

            launch_agent(
                task="test",
                cwd=str(bobi_install.repo_path),
                workflow_name="adhoc",
                subscribe=["org/repo", "slack:T123"],
            )

            script_arg = mock_det.call_args[0][1][0]
            args = json.loads(script_arg)
            assert args["subscribe"] == ["org/repo", "slack:T123"]
