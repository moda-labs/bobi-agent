"""Integration tests for every bobi CLI command.

Exercises each command against the isolated bobi_env install.
No running manager or Claude CLI needed — these verify that commands
parse arguments, read config, and produce sensible output without crashing.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest


class TestStatus:

    def test_status_runs(self, cli_run):
        result = cli_run("status")
        assert result.returncode == 0

    def test_status_shows_no_active_engineers(self, cli_run):
        result = cli_run("status")
        assert "none active" in result.stdout.lower() or "stopped" in result.stdout.lower()


class TestDoctor:

    def test_doctor_runs(self, cli_run):
        result = cli_run("doctor", timeout=30)
        # Exit code 1 is expected when event server isn't running
        assert result.returncode in (0, 1)
        assert "config" in result.stdout.lower() or "claude" in result.stdout.lower()

    def test_doctor_checks_config(self, cli_run):
        result = cli_run("doctor", timeout=30)
        assert "config" in result.stdout.lower() or "ok" in result.stdout.lower()


class TestEvents:

    def test_events_runs(self, cli_run):
        result = cli_run("events")
        assert result.returncode == 0

    def test_events_decisions_only(self, cli_run):
        result = cli_run("events", "--decisions-only")
        assert result.returncode == 0

    def test_events_with_tail(self, cli_run):
        result = cli_run("events", "--tail", "5")
        assert result.returncode == 0


class TestAgentsList:

    def test_agents_list_runs(self, cli_run):
        result = cli_run("agents", "list")
        assert result.returncode == 0

    def test_subagents_show_nonexistent(self, cli_run):
        result = cli_run("subagents", "show", "NONEXISTENT-999")
        # Should fail gracefully
        assert result.returncode == 0
        assert "no sub-agent found" in result.stdout.lower()

    def test_subagents_cancel_nonexistent(self, cli_run):
        result = cli_run("subagents", "cancel", "NONEXISTENT-999")
        assert result.returncode == 0
        assert "no running sub-agent" in result.stdout.lower()


class TestAgentsLaunch:

    def test_launch_missing_workflow(self, cli_run):
        result = cli_run(
            "subagents", "launch",
            "--role", "engineer", "--task", "X",
        )
        assert result.returncode != 0

    def test_launch_missing_role(self, cli_run):
        result = cli_run(
            "subagents", "launch",
            "-w", "adhoc", "--task", "X",
        )
        assert result.returncode != 0


class TestWorkflows:

    def test_workflows_list(self, cli_run):
        result = cli_run("workflows", "list")
        assert result.returncode == 0

    def test_workflows_status(self, cli_run):
        result = cli_run("workflows", "status")
        assert result.returncode == 0

    def test_workflows_validate_valid(self, bobi_env, cli_run):
        wf_file = bobi_env.workflows_dir / "test-valid.yaml"
        wf_file.write_text(textwrap.dedent("""\
            name: test-valid
            trigger: "test trigger"
            steps:
              - name: step1
                prompt: "Do the thing"
        """))
        result = cli_run("workflows", "validate", str(wf_file))
        assert result.returncode == 0

    def test_workflows_validate_invalid(self, bobi_env, cli_run):
        bad_file = bobi_env.state_dir / "bad-workflow.yaml"
        bad_file.write_text("not: a: valid: workflow: [[[")
        result = cli_run("workflows", "validate", str(bad_file))
        assert result.returncode != 0


class TestRoles:

    def test_roles_list(self, cli_run):
        result = cli_run("roles", "list")
        assert result.returncode == 0


class TestMonitors:

    def test_monitors_list(self, cli_run):
        result = cli_run("monitors", "list")
        assert result.returncode == 0

    def test_monitors_add(self, bobi_env, cli_run):
        result = cli_run(
            "monitors", "add", "test-monitor",
            "--interval", "15m",
            "--description", "Test monitor for integration tests",
        )
        assert result.returncode == 0
        assert "test-monitor" in result.stdout

        monitors_file = bobi_env.package_dir / "monitors.yaml"
        assert monitors_file.exists()
        assert "test-monitor" in monitors_file.read_text()

    def test_monitors_remove(self, bobi_env, cli_run):
        cli_run(
            "monitors", "add", "remove-me",
            "--interval", "10m",
            "--description", "Will be removed",
        )
        result = cli_run("monitors", "remove", "remove-me")
        assert result.returncode == 0

    def test_monitors_pause_writes_override(self, cli_run):
        result = cli_run("monitors", "pause", "some-monitor")
        assert result.returncode == 0
        assert "paused" in result.stdout.lower()


class TestTranscript:

    def test_transcript_sessions(self, cli_run):
        result = cli_run("transcript", "sessions")
        assert result.returncode == 0

    def test_transcript_search(self, cli_run):
        result = cli_run("transcript", "search", "test query")
        assert result.returncode == 0

    def test_transcript_show_nonexistent(self, cli_run):
        result = cli_run("transcript", "show", "nonexistent-session")
        # Should handle gracefully
        assert result.returncode == 0 or "not found" in (result.stdout + result.stderr).lower()


class TestSlackReply:

    def test_slack_reply_requires_args(self, cli_run):
        result = cli_run("slack-reply", "hello")
        assert result.returncode != 0
        assert "workspace" in result.stderr.lower() or "required" in result.stderr.lower()


class TestMachineScopedCLI:
    """Top-level commands are machine-scoped; runtime commands require
    `bobi agent <name> ...`."""

    @staticmethod
    def _clean_env(tmp_path):
        """Env dict without BOBI_ROOT and with an isolated BOBI_HOME."""
        env = {**os.environ}
        env.pop("BOBI_ROOT", None)
        env["BOBI_HOME"] = str(tmp_path / "home")
        return env

    def test_top_level_runtime_command_removed(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "bobi.cli", "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path), env=self._clean_env(tmp_path),
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "no such command" in combined.lower()
        assert "Traceback" not in combined

    def test_agents_list_is_machine_scoped(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "bobi.cli", "agents", "list"],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path), env=self._clean_env(tmp_path),
        )
        assert result.returncode == 0
        assert "no bobi agents installed" in result.stdout.lower()

    def test_missing_named_agent_errors_cleanly(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "bobi.cli", "agent", "missing", "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path), env=self._clean_env(tmp_path),
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "bobi agent 'missing' is not installed" in combined.lower()
        assert "Traceback" not in combined
