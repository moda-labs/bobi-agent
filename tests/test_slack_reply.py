"""Tests for slack-reply CLI command."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from textwrap import dedent

from click.testing import CliRunner

from modastack.cli import main


def _setup_project(tmp_path, monkeypatch, slack_bot_token="xoxb-test"):
    """Set up project config with a Slack bot token."""
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir(parents=True)
    if slack_bot_token:
        yaml = (
            "entry_point: manager\n"
            "services:\n"
            "  - name: slack\n"
            "    credentials:\n"
            f"      bot_token: '{slack_bot_token}'\n"
        )
    else:
        yaml = "entry_point: manager\n"
    (config_dir / "agent.yaml").write_text(yaml)
    monkeypatch.chdir(tmp_path)


class TestSlackReplyCommand:

    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "D456", "Hello world",
        ])
        assert result.exit_code == 0
        assert "Sent to D456" in result.output

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["channel"] == "D456"
        assert body["text"] == "Hello world"
        assert "thread_ts" not in body

    @patch("urllib.request.urlopen")
    def test_with_thread(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "C789",
            "-t", "1780165787.159589", "Thread reply",
        ])
        assert result.exit_code == 0

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["thread_ts"] == "1780165787.159589"

    def test_missing_token(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch, slack_bot_token="")

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T_NONE", "-c", "D456", "Hello",
        ])
        assert result.exit_code != 0
        assert "bot token" in result.output.lower()

    @patch("urllib.request.urlopen")
    def test_markdown_conversion(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "D456",
            "**bold** and [link](https://example.com)",
        ])
        assert result.exit_code == 0

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert "*bold*" in body["text"]
        assert "<https://example.com|link>" in body["text"]

    @patch("urllib.request.urlopen")
    def test_escaped_newlines_become_real(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "D456",
            "line one\\nline two\\ttabbed",
        ])
        assert result.exit_code == 0

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["text"] == "line one\nline two\ttabbed"
        assert "\\n" not in body["text"]

    @patch("urllib.request.urlopen")
    def test_real_newlines_preserved(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "D456", "line one\nline two",
        ])
        assert result.exit_code == 0

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["text"] == "line one\nline two"

    @patch("urllib.request.urlopen")
    def test_slack_api_error(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": False, "error": "channel_not_found"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "D456", "Hello",
        ])
        assert result.exit_code != 0
        assert "Slack" in result.output and "error" in result.output.lower()
