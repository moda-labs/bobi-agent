"""Tests for CLI commands."""

import json
from unittest.mock import patch, MagicMock

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


# --- modastack workflows list ------------------------------------------------


def test_workflow_list_shows_workflows():
    runner = CliRunner()
    result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0
    assert "issue-lifecycle" in result.output
    assert "adhoc" in result.output


def test_workflow_list_no_errors():
    """Every workflow YAML should load without errors."""
    runner = CliRunner()
    result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0


# --- modastack agents (unified command) --------------------------------------


class TestAgentCommand:
    def test_adhoc_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-42") as mock:
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--repo", str(tmp_path), "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-adhoc-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"
        assert mock.call_args[1]["role"] == "engineer"

    def test_issue_lifecycle_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.subagent.launch_agent", return_value="wf-issue-lifecycle-42") as mock:
            result = runner.invoke(main, [
                "agents", "launch", "-w", "issue-lifecycle", "--role", "engineer",
                "--repo", str(tmp_path), "--task", "Work on #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-issue-lifecycle-42" in result.output
        assert mock.call_args[1]["workflow_name"] == "issue-lifecycle"

    def test_workflow_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agents", "launch", "--role", "engineer", "--repo", "/tmp", "--task", "X"])
        assert result.exit_code != 0

    def test_role_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agents", "launch", "-w", "adhoc", "--repo", "/tmp", "--task", "X"])
        assert result.exit_code != 0
        assert "--role" in result.output

    def test_invalid_role(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agents", "launch", "-w", "adhoc", "--role", "nonexistent",
            "--repo", str(tmp_path), "--task", "X",
        ])
        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_wait_mode_runs_check(self):
        runner = CliRunner()
        check = CheckResult(success=True, finding=False)
        with patch("modastack.subagent.run_check_blocking", return_value=check):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0

    def test_requires_repo(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agents", "launch", "-w", "adhoc", "--role", "engineer", "--task", "do a thing",
        ])
        assert result.exit_code != 0
        assert "--repo is required" in result.output

    def test_passes_requested_by(self, tmp_path):
        runner = CliRunner()
        req = '{"from":"Alice","channel":"C1"}'
        with patch("modastack.subagent.launch_agent", return_value="wf-adhoc-1") as mock:
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--repo", str(tmp_path), "--task", "Fix #1",
                "--requested-by", req,
            ])
        assert result.exit_code == 0
        assert mock.call_args[1]["requested_by"] == {"from": "Alice", "channel": "C1"}


# --- modastack login / logout ------------------------------------------------


class TestLoginLogout:
    def test_login_triggers_oauth_flow(self, tmp_path):
        runner = CliRunner()
        auth_file = tmp_path / "auth.yaml"

        mock_state = MagicMock()
        mock_state.github_username = "testuser"

        auth_config_resp = MagicMock()
        auth_config_resp.read.return_value = json.dumps({"client_id": "cid", "mode": "remote"}).encode()
        auth_config_resp.__enter__ = lambda s: s
        auth_config_resp.__exit__ = MagicMock(return_value=False)

        with patch("modastack.auth.github_login", return_value=mock_state) as mock_login, \
             patch("modastack.auth.AUTH_PATH", auth_file), \
             patch("urllib.request.urlopen", return_value=auth_config_resp):
            result = runner.invoke(main, ["login", "--event-server", "https://events.example.com"])
        assert result.exit_code == 0
        assert "testuser" in result.output

    def test_logout_clears_auth(self):
        runner = CliRunner()
        with patch("modastack.auth.is_authenticated", return_value=True), \
             patch("modastack.auth.clear_auth") as mock_clear:
            result = runner.invoke(main, ["logout"])
        assert result.exit_code == 0
        mock_clear.assert_called_once()
        assert "Logged out" in result.output

    def test_logout_when_not_logged_in(self):
        runner = CliRunner()
        with patch("modastack.auth.is_authenticated", return_value=False):
            result = runner.invoke(main, ["logout"])
        assert result.exit_code == 0
        assert "Not logged in" in result.output
