"""Tests for slack-reply CLI command."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from textwrap import dedent

import httpx
from click.testing import CliRunner

from modastack.cli import main
from modastack import http as pooled


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


def _mock_client(handler):
    """Create an httpx.Client with a MockTransport backed by *handler*."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """Default handler that returns {"ok": True}."""
    return httpx.Response(200, json={"ok": True})


class TestSlackReplyCommand:

    def test_success(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "D456", "Hello world",
            ])
        assert result.exit_code == 0
        assert "Sent to D456" in result.output

        assert len(requests_made) >= 1
        req = requests_made[0]
        body = json.loads(req.content)
        assert body["channel"] == "D456"
        assert body["text"] == "Hello world"
        assert "thread_ts" not in body

    def test_with_thread(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "C789",
                "-t", "1780165787.159589", "Thread reply",
            ])
        assert result.exit_code == 0

        req = requests_made[0]
        body = json.loads(req.content)
        assert body["thread_ts"] == "1780165787.159589"

    def test_missing_token(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch, slack_bot_token="")

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T_NONE", "-c", "D456", "Hello",
        ])
        assert result.exit_code != 0
        assert "bot token" in result.output.lower()

    def test_markdown_conversion(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "D456",
                "**bold** and [link](https://example.com)",
            ])
        assert result.exit_code == 0

        req = requests_made[0]
        body = json.loads(req.content)
        assert "*bold*" in body["text"]
        assert "<https://example.com|link>" in body["text"]

    def test_escaped_newlines_become_real(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "D456",
                "line one\\nline two\\ttabbed",
            ])
        assert result.exit_code == 0

        req = requests_made[0]
        body = json.loads(req.content)
        assert body["text"] == "line one\nline two\ttabbed"
        assert "\\n" not in body["text"]

    def test_real_newlines_preserved(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "D456", "line one\nline two",
            ])
        assert result.exit_code == 0

        req = requests_made[0]
        body = json.loads(req.content)
        assert body["text"] == "line one\nline two"

    def test_slack_api_error(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

        mock_client = _mock_client(handler)
        with patch.object(pooled, '_client', mock_client):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "D456", "Hello",
            ])
        assert result.exit_code != 0
        assert "Slack" in result.output and "error" in result.output.lower()
