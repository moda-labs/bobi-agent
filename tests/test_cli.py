"""Tests for CLI commands."""

import json
from unittest.mock import patch

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


def test_agent_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["agent", "--version"])
    assert result.exit_code == 0
    assert "modastack" in result.output
    assert __version__ in result.output



def test_post_event_splits_source_and_type():
    """_post_event splits 'source/type' and POSTs the right payload."""
    from modastack.cli import _post_event

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok = _post_event("monitor/deploy.down", {"summary": "down"})

    assert ok is True
    assert captured["url"] == "http://localhost:8095/api/event"
    assert captured["body"]["source"] == "monitor"
    assert captured["body"]["type"] == "deploy.down"
    assert captured["body"]["data"] == {"summary": "down"}


def test_post_event_defaults_source_when_no_slash():
    from modastack.cli import _post_event

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout=10):
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _post_event("deploy_down", {})

    assert captured["body"]["source"] == "monitor"
    assert captured["body"]["type"] == "deploy_down"


def test_post_event_returns_false_on_connection_error():
    from modastack.cli import _post_event
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("nope")):
        assert _post_event("monitor/x", {}) is False


# --- modastack workflow list ------------------------------------------------


def test_workflow_list_shows_workflows():
    runner = CliRunner()
    result = runner.invoke(main, ["workflow", "list"])
    assert result.exit_code == 0
    assert "issue-lifecycle" in result.output
    assert "adhoc" in result.output


def test_workflow_list_no_errors():
    """Every workflow YAML should load without errors."""
    runner = CliRunner()
    result = runner.invoke(main, ["workflow", "list"])
    assert result.exit_code == 0


# --- modastack agent (unified command) --------------------------------------


class TestAgentCommand:
    def test_adhoc_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="eng-42") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--role", "engineer",
                "--repo", str(tmp_path), "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "eng-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"
        assert mock.call_args[1]["role"] == "engineer"

    def test_issue_lifecycle_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-42") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "issue-lifecycle", "--role", "engineer",
                "--repo", str(tmp_path), "--issue", "42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-issue-lifecycle-42" in result.output
        assert mock.call_args[1]["workflow_name"] == "issue-lifecycle"

    def test_neither_task_nor_workflow(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--role", "engineer", "--repo", "/tmp", "--task", "X"])
        assert result.exit_code != 0

    def test_role_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "-w", "adhoc", "--repo", "/tmp", "--task", "X"])
        assert result.exit_code != 0
        assert "--role" in result.output

    def test_invalid_role(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "-w", "adhoc", "--role", "nonexistent",
            "--repo", str(tmp_path), "--task", "X",
        ])
        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_wait_mode_runs_check(self):
        runner = CliRunner()
        check = CheckResult(success=True, finding=False)
        with patch("modastack.subagent.run_check_blocking", return_value=check):
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--role", "engineer",
                "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0

    def test_requires_repo(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "-w", "adhoc", "--role", "engineer", "--task", "do a thing",
        ])
        assert result.exit_code != 0
        assert "--repo is required" in result.output

    def test_passes_requested_by(self, tmp_path):
        runner = CliRunner()
        req = '{"from":"Alice","channel":"C1"}'
        with patch("modastack.subagent.launch_agent", return_value="eng-1") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--role", "engineer",
                "--repo", str(tmp_path), "--task", "Fix #1",
                "--requested-by", req,
            ])
        assert result.exit_code == 0
        assert mock.call_args[1]["requested_by"] == {"from": "Alice", "channel": "C1"}

    def test_workflow_passes_issue(self, tmp_path):
        """--issue must reach launch_agent so the run targets that issue."""
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-34") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "issue-lifecycle", "--role", "engineer",
                "--repo", str(tmp_path), "--issue", "34",
            ])
        assert result.exit_code == 0, result.output
        assert mock.call_args[1]["issue"] == "34"

    def test_event_json_overrides_issue(self, tmp_path):
        """A full event payload binds the run to its issue id and title."""
        runner = CliRunner()
        event = json.dumps({"issue_id": "55", "title": "Add rate limiting"})
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-55") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "issue-lifecycle", "--role", "engineer",
                "--repo", str(tmp_path), "--issue", "1", "--event-json", event,
            ])
        assert result.exit_code == 0, result.output
        assert mock.call_args[1]["issue"] == "55"
        assert mock.call_args[1]["title"] == "Add rate limiting"

    def test_collision_reports_existing_run(self, tmp_path):
        """A run colliding with an active (repo, issue) exits non-zero."""
        from modastack.subagent import RunCollision
        from modastack.sdk import SessionEntry
        existing = SessionEntry(name="wf-issue-lifecycle-34", issue_id="34",
                                status="running")
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", side_effect=RunCollision(existing)):
            result = runner.invoke(main, [
                "agent", "-w", "issue-lifecycle", "--role", "engineer",
                "--repo", str(tmp_path), "--issue", "34",
            ])
        assert result.exit_code != 0
        assert "already active" in result.output
        assert "34" in result.output


# --- modastack cancel --------------------------------------------------------


class TestCancelCommand:
    def test_cancel_success(self):
        runner = CliRunner()
        with patch("modastack.subagent.cancel_run", return_value=["wf-issue-lifecycle-42"]):
            result = runner.invoke(main, ["cancel", "42"])
        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert "wf-issue-lifecycle-42" in result.output

    def test_cancel_no_match(self):
        runner = CliRunner()
        with patch("modastack.subagent.cancel_run", return_value=[]):
            result = runner.invoke(main, ["cancel", "nope"])
        assert result.exit_code == 1
        assert "No active session" in result.output
