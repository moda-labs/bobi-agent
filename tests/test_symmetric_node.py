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

from modastack.events.subscriptions import build_subscriptions
from modastack.events.drain import drain_loop, DRAIN_INTERVAL
from modastack.events.client import format_event_for_manager, event_queue


# ---------------------------------------------------------------------------
# modastack.events.subscriptions
# ---------------------------------------------------------------------------


class TestBuildSubscriptions:

    def test_github_repo(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("github:\n  repo: org/myrepo\n")
        subs = build_subscriptions(tmp_path)
        assert "org/myrepo" in subs

    def test_slack_channel_scoped(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "github:\n  repo: org/myrepo\n"
            "slack:\n  workspace_id: T123\n  channel: C456\n"
        )
        subs = build_subscriptions(tmp_path)
        assert "slack:T123:C456" in subs
        assert "slack:T123" not in subs

    def test_linear_team(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "task_tracking:\n  system: linear\n"
            "linear:\n  team: MOD\n"
        )
        subs = build_subscriptions(tmp_path)
        assert "linear:MOD" in subs

    def test_fallback_to_dir_name(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}\n")
        subs = build_subscriptions(tmp_path)
        assert tmp_path.name in subs

    def test_missing_config(self, tmp_path):
        subs = build_subscriptions(tmp_path)
        assert tmp_path.name in subs


# ---------------------------------------------------------------------------
# modastack.events.drain
# ---------------------------------------------------------------------------


class TestDrainLoop:

    def test_batches_and_delivers(self):
        queue = SimpleQueue()
        delivered = []

        def mock_formatter(event):
            return f"formatted:{event['type']}"

        with patch("modastack.inbox.deliver") as mock_deliver:
            mock_deliver.return_value = (True, "")

            thread = threading.Thread(
                target=drain_loop,
                args=("test-session", queue, mock_formatter),
                daemon=True,
            )
            thread.start()

            queue.put({"type": "task.opened", "source": "github"})
            queue.put({"type": "pr.opened", "source": "github"})

            time.sleep(DRAIN_INTERVAL + 1)

            assert mock_deliver.called
            call_args = mock_deliver.call_args
            assert call_args[0][0] == "test-session"
            assert "formatted:task.opened" in call_args[0][1]

    def test_slack_events_delivered_separately(self):
        queue = SimpleQueue()

        with patch("modastack.inbox.deliver") as mock_deliver:
            mock_deliver.return_value = (True, "")

            thread = threading.Thread(
                target=drain_loop,
                args=("test-session", queue, format_event_for_manager),
                daemon=True,
            )
            thread.start()

            queue.put({"type": "task.opened", "source": "github",
                       "data": {"issue_id": "1"}})
            queue.put({"type": "slack.dm", "source": "slack",
                       "data": {"text": "hi"}})

            time.sleep(DRAIN_INTERVAL + 1)

            assert mock_deliver.call_count >= 2


# ---------------------------------------------------------------------------
# backward compat: old imports still work
# ---------------------------------------------------------------------------


class TestBackwardCompat:

    def test_direct_imports_work(self):
        from modastack.events.client import (
            EventServerClient,
            event_queue as eq,
            format_event_for_manager as fmt,
        )
        assert EventServerClient is not None
        assert eq is event_queue
        assert fmt is format_event_for_manager

    def test_server_imports_work(self):
        from modastack.events.server import ensure_running, register
        assert callable(ensure_running)
        assert callable(register)

    def test_build_subscriptions_direct(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("github:\n  repo: org/repo\n")

        from modastack.events.subscriptions import build_subscriptions
        subs = build_subscriptions(tmp_path)
        assert "org/repo" in subs


# ---------------------------------------------------------------------------
# agent config loading (Phase 3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# prompt resolver (Phase 4)
# ---------------------------------------------------------------------------


class TestPromptResolver:

    def test_resolve_manager_prompt_loads_base(self, tmp_path):
        from modastack.prompts.resolver import resolve_manager_prompt
        prompt = resolve_manager_prompt(tmp_path)
        assert len(prompt) > 0

    def test_resolve_manager_prompt_includes_repo_override(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "manager.md").write_text("Custom policy: always review PRs.")
        from modastack.prompts.resolver import resolve_manager_prompt
        prompt = resolve_manager_prompt(tmp_path)
        assert "Custom policy: always review PRs." in prompt
        assert f"{tmp_path.name} policies" in prompt

    def test_list_workflows_returns_string(self, tmp_path):
        from modastack.prompts.resolver import list_workflows
        result = list_workflows(tmp_path)
        assert isinstance(result, str)

    def test_list_workflows_finds_builtin(self):
        from modastack.prompts.resolver import list_workflows
        result = list_workflows(Path("/tmp/nonexistent"))
        assert isinstance(result, str)


class TestAgentConfig:

    def test_load_agent_config_from_default_path(self, tmp_path):
        from modastack.cli import _load_agent_config
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(dedent("""
            role: manager
            persistent: true
            subscribe:
              - moda-labs/modastack
              - slack:T123
            monitors: true
        """))
        config = _load_agent_config(tmp_path)
        assert config["role"] == "manager"
        assert config["persistent"] is True
        assert "moda-labs/modastack" in config["subscribe"]
        assert "slack:T123" in config["subscribe"]
        assert config["monitors"] is True

    def test_load_agent_config_explicit_path(self, tmp_path):
        from modastack.cli import _load_agent_config
        custom = tmp_path / "custom-agent.yaml"
        custom.write_text("role: assistant\npersistent: false\n")
        config = _load_agent_config(tmp_path, str(custom))
        assert config["role"] == "assistant"
        assert config["persistent"] is False

    def test_load_agent_config_missing_returns_none(self, tmp_path):
        from modastack.cli import _load_agent_config
        config = _load_agent_config(tmp_path)
        assert config is None

    def test_load_agent_config_empty_file(self, tmp_path):
        from modastack.cli import _load_agent_config
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("")
        config = _load_agent_config(tmp_path)
        assert config == {}


# ---------------------------------------------------------------------------
# --subscribe on agent launch (Phase 2)
# ---------------------------------------------------------------------------


class TestSubscribeFlag:

    def test_subscribe_implies_persistent(self):
        from click.testing import CliRunner
        from modastack.cli import main

        with patch("modastack.subagent.launch_agent") as mock_launch, \
             patch("modastack.cli._detect_project_root") as mock_root, \
             patch("modastack.prompts.resolver.validate_role", return_value=True):
            mock_root.return_value = Path("/tmp/project")
            mock_launch.return_value = "test-session"

            runner = CliRunner()
            result = runner.invoke(main, [
                "agents", "launch",
                "-w", "adhoc",
                "--role", "manager",
                "--task", "watch events",
                "--subscribe", "moda-labs/modastack",
            ])

            assert result.exit_code == 0
            call_kwargs = mock_launch.call_args[1]
            assert call_kwargs["persistent"] is True
            assert "moda-labs/modastack" in call_kwargs["subscribe"]

    def test_subscribe_multiple_topics(self):
        from click.testing import CliRunner
        from modastack.cli import main

        with patch("modastack.subagent.launch_agent") as mock_launch, \
             patch("modastack.cli._detect_project_root") as mock_root, \
             patch("modastack.prompts.resolver.validate_role", return_value=True):
            mock_root.return_value = Path("/tmp/project")
            mock_launch.return_value = "test-session"

            runner = CliRunner()
            result = runner.invoke(main, [
                "agents", "launch",
                "-w", "adhoc",
                "--role", "manager",
                "--task", "watch",
                "--subscribe", "org/repo",
                "--subscribe", "slack:T123",
            ])

            assert result.exit_code == 0
            call_kwargs = mock_launch.call_args[1]
            assert call_kwargs["subscribe"] == ["org/repo", "slack:T123"]

    def test_launch_agent_passes_subscribe_to_args(self):
        from modastack.subagent import launch_agent

        with patch("modastack.subagent._launch_detached", return_value=12345) as mock_det, \
             patch("modastack.subagent.get_registry") as mock_reg:
            mock_reg.return_value = MagicMock()
            mock_reg.return_value.get.return_value = None

            launch_agent(
                task="test",
                cwd="/tmp",
                workflow_name="adhoc",
                subscribe=["org/repo", "slack:T123"],
            )

            script_arg = mock_det.call_args[0][1][0]
            args = json.loads(script_arg)
            assert args["subscribe"] == ["org/repo", "slack:T123"]
