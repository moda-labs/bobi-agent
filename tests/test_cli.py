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
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-42") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--repo", str(tmp_path), "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-adhoc-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"

    def test_issue_lifecycle_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-42") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "issue-lifecycle",
                "--repo", str(tmp_path), "--task", "Work on #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-issue-lifecycle-42" in result.output
        assert mock.call_args[1]["workflow_name"] == "issue-lifecycle"

    def test_workflow_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "--repo", "/tmp", "--task", "X"])
        assert result.exit_code != 0

    def test_wait_mode_runs_check(self):
        runner = CliRunner()
        check = CheckResult(success=True, finding=False)
        with patch("modastack.subagent.run_check_blocking", return_value=check):
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0

    def test_requires_repo(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "-w", "adhoc", "--task", "do a thing"])
        assert result.exit_code != 0
        assert "--repo is required" in result.output

    def test_passes_requested_by(self, tmp_path):
        runner = CliRunner()
        req = '{"from":"Alice","channel":"C1"}'
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-1") as mock:
            result = runner.invoke(main, [
                "agent", "-w", "adhoc", "--repo", str(tmp_path), "--task", "Fix #1",
                "--requested-by", req,
            ])
        assert result.exit_code == 0
        assert mock.call_args[1]["requested_by"] == {"from": "Alice", "channel": "C1"}

