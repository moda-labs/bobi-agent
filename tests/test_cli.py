"""Tests for CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from modastack.__version__ import __version__
from modastack.cli import main
from modastack.subagent import CheckResult


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "modastack" in result.output
    assert __version__ in result.output


def _fresh_publish():
    """Clear the memoized event-server URL between tests."""
    from modastack.events import publish
    publish._es_url_cache.clear()
    return publish


def test_post_event_posts_to_event_server():
    publish = _fresh_publish()

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"delivered_to": 0}).encode()

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("modastack.config.Config.load") as mock_pc:
        mock_pc.return_value = type("PC", (), {"event_server_url": "https://events.test"})()
        ok = publish.post_event("monitor/deploy.down", {"summary": "down"},
                                project_path=Path("/tmp/repo"))

    assert ok is True
    assert captured["url"] == "https://events.test/events/deploy.down"
    assert captured["body"]["source"] == "monitor"
    assert captured["body"]["payload"] == {"summary": "down"}


def test_post_event_defaults_source_when_no_slash():
    publish = _fresh_publish()

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"delivered_to": 1}).encode()

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("modastack.config.Config.load") as mock_pc:
        mock_pc.return_value = type("PC", (), {"event_server_url": "http://localhost:8080"})()
        publish.post_event("deploy_down", {}, project_path=Path("/tmp/repo"))

    assert captured["body"]["source"] == "monitor"
    assert "deploy_down" in captured["url"]


def test_post_event_returns_false_on_connection_error():
    publish = _fresh_publish()
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("nope")), \
         patch("modastack.config.Config.load") as mock_pc:
        mock_pc.return_value = type("PC", (), {"event_server_url": "http://localhost:8080"})()
        assert publish.post_event("monitor/x", {}, project_path=Path("/tmp/repo")) is False


# --- modastack workflows list ------------------------------------------------


def test_workflow_list_shows_installed_workflows(modastack_install):
    """Workflows resolve only from the installed pack, not from the framework."""
    runner = CliRunner()
    with patch("modastack.cli._detect_project_root",
               return_value=modastack_install.repo_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0
    # The modastack_install fixture installs an adhoc workflow in .modastack/workflows/
    assert "adhoc" in result.output


def test_workflow_list_empty_without_pack(tmp_path):
    """Without an installed pack, no workflows should resolve."""
    runner = CliRunner()
    with patch("modastack.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0
    assert "No workflows loaded" in result.output


def test_workflow_list_no_errors(tmp_path):
    (tmp_path / ".modastack" / "workflows").mkdir(parents=True)
    (tmp_path / ".modastack" / "agent.yaml").write_text("name: t\n")
    runner = CliRunner()
    with patch("modastack.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0


# --- modastack agents (unified command) --------------------------------------


class TestAgentCommand:
    def test_adhoc_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-42") as mock, \
             patch("modastack.cli._detect_project_root", return_value=tmp_path), \
             patch("modastack.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-adhoc-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"
        assert mock.call_args[1]["role"] == "engineer"

    def test_issue_lifecycle_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-42") as mock, \
             patch("modastack.cli._detect_project_root", return_value=tmp_path), \
             patch("modastack.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "issue-lifecycle", "--role", "engineer",
                "--task", "Work on #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-issue-lifecycle-42" in result.output
        assert mock.call_args[1]["workflow_name"] == "issue-lifecycle"

    def test_workflow_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agents", "launch", "--role", "engineer", "--task", "X"])
        assert result.exit_code != 0

    def test_role_required(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.paths._root", tmp_path):
            result = runner.invoke(main, ["agents", "launch", "-w", "adhoc", "--task", "X"])
        assert result.exit_code != 0
        assert "--role" in result.output

    def test_invalid_role(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "nonexistent",
                "--task", "X",
            ])
        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_wait_mode_runs_check(self, tmp_path):
        runner = CliRunner()
        check = CheckResult(success=True, finding=False)
        with patch("modastack.subagent.run_check_blocking", return_value=check), \
             patch("modastack.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0

    def test_requires_repo(self):
        import click
        runner = CliRunner()
        with patch("modastack.cli._detect_project_root",
                   side_effect=click.UsageError("no Modastack installation found above /x")):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer", "--task", "do a thing",
            ])
        assert result.exit_code != 0
        assert "no modastack installation" in result.output.lower()

    def test_passes_requested_by(self, tmp_path):
        runner = CliRunner()
        req = '{"from":"Alice","channel":"C1"}'
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-1") as mock, \
             patch("modastack.cli._detect_project_root", return_value=tmp_path), \
             patch("modastack.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--task", "Fix #1",
                "--requested-by", req,
            ])
        assert result.exit_code == 0
        assert mock.call_args[1]["requested_by"] == {"from": "Alice", "channel": "C1"}


# --- monitor event subscription -----------------------------------------------


class TestMonitorEventSubscription:
    """Regression for #216: coordinator must subscribe to monitor events
    even when all event services use native adapters."""

    def test_monitor_events_subscribed_from_registry(self, modastack_install):
        """Monitor events from defaults.yaml must appear in the subscribe list,
        regardless of adapter configuration."""
        collected_subscribe = []

        def fake_start_event_subscription(session_name, subscribe, project_path):
            collected_subscribe.extend(subscribe)

        with patch("modastack.subagent._start_event_subscription",
                   side_effect=fake_start_event_subscription) as mock_sub, \
             patch("modastack.cli._manager_session_name", return_value="moda-director-repo"), \
             patch("modastack.monitors.scheduler.MonitorScheduler"), \
             patch("modastack.prompts.resolver.build_startup_prompt", return_value="go"), \
             patch("modastack.subagent.spawn_adhoc"), \
             patch("modastack.events.subscriptions.discover_subscriptions",
                   return_value=["github:o/r"]):
            from modastack.config import Config
            cfg = Config.load(modastack_install.repo_path)
            from modastack.cli import _run_from_config
            _run_from_config(modastack_install.repo_path, cfg)

        mock_sub.assert_called_once()
        assert "monitor/test" in collected_subscribe, (
            "Monitor event topic from defaults.yaml was not subscribed"
        )


# --- modastack events (malformed line handling) --------------------------------


class TestEventsCommand:
    """The events command must not crash on malformed JSONL lines."""

    def test_skips_malformed_lines_in_events_jsonl(self, tmp_path):
        state = tmp_path / ".modastack" / "state"
        state.mkdir(parents=True)
        good = {"timestamp": "2026-01-01T00:00:00", "source": "github", "type": "push", "data": {}}
        # Write a good line, a corrupted line, and another good line
        (state / "events.jsonl").write_text(
            json.dumps(good) + "\n"
            + "NOT VALID JSON\n"
            + json.dumps({**good, "type": "pr"}) + "\n"
        )
        runner = CliRunner()
        with patch("modastack.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "push" in result.output
        assert "pr" in result.output
        assert "1 malformed" in result.output

    def test_skips_malformed_lines_in_decisions_jsonl(self, tmp_path):
        state = tmp_path / ".modastack" / "state"
        state.mkdir(parents=True)
        good = {"timestamp": "2026-01-01T00:00:00", "actions": [{"type": "deploy"}], "reasoning": "ship it"}
        (state / "decisions.jsonl").write_text(
            json.dumps(good) + "\n"
            + "CORRUPTED\n"
        )
        runner = CliRunner()
        with patch("modastack.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "deploy" in result.output
        assert "1 malformed" in result.output


class TestSetupCommand:
    """The setup command wires the CLI to the interactive REPL."""

    @pytest.fixture(autouse=True)
    def _claude_present(self, monkeypatch):
        # The command preflights the Claude CLI; unit tests must pass on
        # machines without it.
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def test_missing_claude_cli_fails_with_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("modastack.sdk.get_cli_path",
                            lambda: "/nonexistent/claude")
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["setup"])
        assert result.exit_code != 0
        assert "Claude Code CLI" in result.output

    def test_interrupted_setup_requires_confirmation(self, tmp_path, monkeypatch):
        from modastack.setup.state import SetupState, Stage

        called = {}
        monkeypatch.setattr("modastack.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            SetupState(stage=Stage.DESIGN, team_name="t").save(Path(fs))
            declined = runner.invoke(main, ["setup"], input="n\n")
            assert declined.exit_code != 0
            assert "--resume" in declined.output
            assert "ran" not in called

    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "--resume" in result.output

    def test_runs_setup_against_literal_cwd(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run_setup(project_path, model=None, resume=False):
            seen.update(project=project_path, model=model, resume=resume)
            return 0

        monkeypatch.setattr("modastack.setup.run_setup", fake_run_setup)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            result = runner.invoke(main, ["setup", "--model", "sonnet"])
        assert result.exit_code == 0, result.output
        assert seen["project"] == Path(fs).resolve()
        assert seen["model"] == "sonnet"
        assert seen["resume"] is False

    def test_existing_install_requires_confirmation(self, tmp_path, monkeypatch):
        called = {}
        monkeypatch.setattr("modastack.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            dot = Path(".modastack")
            dot.mkdir()
            (dot / "agent.yaml").write_text("agent: eng-team\n")
            declined = runner.invoke(main, ["setup"], input="n\n")
            assert declined.exit_code != 0
            assert "ran" not in called
            accepted = runner.invoke(main, ["setup"], input="y\n")
            assert accepted.exit_code == 0, accepted.output
            assert called.get("ran") is True

    def test_resume_skips_confirmation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.setup.run_setup", lambda *a, **k: 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            dot = Path(".modastack")
            dot.mkdir()
            (dot / "agent.yaml").write_text("agent: eng-team\n")
            result = runner.invoke(main, ["setup", "--resume"])
        assert result.exit_code == 0, result.output
