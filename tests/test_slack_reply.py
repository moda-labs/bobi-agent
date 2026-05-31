"""Tests for slack-reply CLI command and multi-workspace config."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from textwrap import dedent

from click.testing import CliRunner

from modastack.cli import main
from modastack.config import GlobalConfig


class TestSlackTokenLookup:

    def test_default_token(self):
        config = GlobalConfig(slack_bot_token="xoxb-default")
        assert config.slack_token_for("") == "xoxb-default"
        assert config.slack_token_for("T_UNKNOWN") == "xoxb-default"

    def test_workspace_token(self):
        config = GlobalConfig(
            slack_bot_token="xoxb-default",
            slack_workspaces={
                "T_WORK1": {"bot_token": "xoxb-work1"},
                "T_WORK2": {"bot_token": "xoxb-work2"},
            },
        )
        assert config.slack_token_for("T_WORK1") == "xoxb-work1"
        assert config.slack_token_for("T_WORK2") == "xoxb-work2"
        assert config.slack_token_for("T_UNKNOWN") == "xoxb-default"
        assert config.slack_token_for("") == "xoxb-default"

    def test_workspace_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

        config = GlobalConfig(
            slack_bot_token="xoxb-default",
            slack_workspaces={"T123": {"bot_token": "xoxb-123"}},
        )
        config.save()

        loaded = GlobalConfig.load()
        assert loaded.slack_token_for("T123") == "xoxb-123"
        assert loaded.slack_token_for("") == "xoxb-default"


class TestSlackReplyCommand:

    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig(slack_bot_token="xoxb-test")
        config.save()

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
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig(slack_bot_token="xoxb-test")
        config.save()

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
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig()
        config.save()

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T_NONE", "-c", "D456", "Hello",
        ])
        assert result.exit_code != 0
        assert "No bot token" in result.output

    @patch("urllib.request.urlopen")
    def test_workspace_specific_token(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig(
            slack_bot_token="xoxb-default",
            slack_workspaces={"T_SPECIAL": {"bot_token": "xoxb-special"}},
        )
        config.save()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T_SPECIAL", "-c", "D456", "Hello",
        ])
        assert result.exit_code == 0

        req = mock_urlopen.call_args[0][0]
        assert "Bearer xoxb-special" in req.headers["Authorization"]

    @patch("urllib.request.urlopen")
    def test_markdown_conversion(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig(slack_bot_token="xoxb-test")
        config.save()

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
    def test_slack_api_error(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
        monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
        config = GlobalConfig(slack_bot_token="xoxb-test")
        config.save()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": False, "error": "channel_not_found"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "INVALID", "Hello",
        ])
        assert result.exit_code != 0
        assert "channel_not_found" in result.output
