"""Tests for the slack-reply and channel-agnostic reply CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from textwrap import dedent

import httpx
from click.testing import CliRunner

from bobi.cli import main
from bobi import paths
from bobi import http as pooled


def _setup_project(tmp_path, monkeypatch, slack_bot_token="xoxb-test"):
    """Set up project config with a Slack bot token."""
    config_dir = paths.package_dir(tmp_path)
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
    paths.agent_yaml_path(tmp_path).write_text(yaml)
    monkeypatch.setenv("BOBI_ROOT", str(tmp_path))


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


class TestReplyCommand:
    """`bobi reply <conversation>` - channel-agnostic front of the same send path (#618)."""

    def test_posts_to_dm_from_ref(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T123:dm:D456", "Hello world",
            ])
        assert result.exit_code == 0
        assert "Sent to D456" in result.output

        body = json.loads(requests_made[0].content)
        assert body["channel"] == "D456"
        assert body["text"] == "Hello world"
        assert "thread_ts" not in body

    def test_posts_into_thread_from_ref(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T123:channel:C789:thread:1780165787.159589",
                "Thread reply",
            ])
        assert result.exit_code == 0

        body = json.loads(requests_made[0].content)
        assert body["channel"] == "C789"
        assert body["thread_ts"] == "1780165787.159589"

    def test_edit_updates_placeholder(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T123:channel:C789:thread:171.42",
                "--edit", "171.99", "Real response",
            ])
        assert result.exit_code == 0
        assert "Updated 171.99 in C789" in result.output

        update_reqs = [r for r in requests_made if "chat.update" in str(r.url)]
        assert len(update_reqs) == 1
        body = json.loads(update_reqs[0].content)
        assert body["ts"] == "171.99"
        assert body["channel"] == "C789"

    def test_reads_text_from_stdin(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        requests_made: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_made.append(request)
            return httpx.Response(200, json={"ok": True})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T123:dm:D456",
            ], input="Hello from stdin\n")
        assert result.exit_code == 0

        body = json.loads(requests_made[0].content)
        assert body["text"] == "Hello from stdin"

    def test_rejects_invalid_ref(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "not-a-ref", "Hello"])
        assert result.exit_code != 0
        assert "Invalid conversation reference" in result.output

    def test_rejects_unsupported_channel(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, [
            "reply", "whatsapp:747:dm:15550001111", "Hello",
        ])
        assert result.exit_code != 0
        assert "Unsupported channel: whatsapp" in result.output

    def test_rejects_empty_text(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "slack:T123:dm:D456"], input="")
        assert result.exit_code != 0
        assert "No text to send" in result.output

    def test_missing_token(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch, slack_bot_token="")
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "slack:T123:dm:D456", "Hello"])
        assert result.exit_code != 0
        assert "bot token" in result.output.lower()

    def test_slack_error_includes_workspace_scope_hint(self, tmp_path, monkeypatch):
        """A cross-workspace ref fails at the Slack API; the error must name
        the ref's workspace so the token mismatch is diagnosable."""
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T_OTHER:channel:C9:thread:1.2", "Hello",
            ])
        assert result.exit_code != 0
        assert "T_OTHER" in result.output
        assert "workspace" in result.output
