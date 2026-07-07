"""Tests for `bobi reply` / `bobi read-conversation` and the deprecated
slack-* shims (#190 Phase 2).

All of these go through the channel gateway: the CLI signs requests with the
instance's bubble key and POSTs to the event server's /channels/* endpoints.
No Slack token is read client-side and no markdown is converted client-side.
"""

import base64
import json
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from bobi import http as pooled
from bobi import paths
from bobi.cli import main
from bobi.config import save_bubble_state


def _setup_project(tmp_path, monkeypatch, *, with_bubble=True):
    config_dir = paths.package_dir(tmp_path)
    config_dir.mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text("entry_point: manager\n")
    monkeypatch.setenv("BOBI_ROOT", str(tmp_path))
    if with_bubble:
        save_bubble_state(tmp_path, "bub_test", "bkey_test")


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _gateway_ok(requests_log, response=None):
    """Handler that records requests and returns a gateway success."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
        return httpx.Response(200, json=response or {"ok": True, "ts": "99.1"})
    return handler


class TestReplyCommand:
    """bobi reply <conversation> — the channel-agnostic reply path."""

    def test_posts_markdown_through_gateway(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T0952RZRZ0X:dm:D0B51JP1N4C", "**Hello** there",
            ])
        assert result.exit_code == 0, result.output
        assert "Sent to slack:T0952RZRZ0X:dm:D0B51JP1N4C" in result.output

        req = reqs[0]
        assert str(req.url).endswith("/channels/send")
        body = json.loads(req.content)
        # Raw markdown goes to the gateway — never converted client-side.
        assert body["text"] == "**Hello** there"
        assert body["conversation"] == "slack:T0952RZRZ0X:dm:D0B51JP1N4C"
        # A reply resolves the response context.
        assert body["mode"] == "final"
        # Bubble-signed request headers.
        assert req.headers["x-moda-bubble"] == "bub_test"
        assert req.headers["x-moda-signature"]
        assert req.headers["x-moda-algo"] == "hmac-sha256"

    def test_edit_sends_edit_ref(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T1:channel:C123:thread:171.42",
                "--edit", "171.99", "Real response",
            ])
        assert result.exit_code == 0, result.output
        assert "Updated 171.99" in result.output
        body = json.loads(reqs[0].content)
        assert body["mode"] == "final"
        assert body["edit_ref"] == "171.99"

    def test_file_upload_sends_b64_payload(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        f = tmp_path / "report.txt"
        f.write_bytes(b"file-bytes")
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T1:channel:C123:thread:171.42",
                "--file", str(f), "--title", "Report", "here you go",
            ])
        assert result.exit_code == 0, result.output
        body = json.loads(reqs[0].content)
        assert body["files"] == [{
            "name": "report.txt",
            "content_b64": base64.b64encode(b"file-bytes").decode(),
            "title": "Report",
        }]
        assert body["text"] == "here you go"

    def test_edit_with_file_resolves_placeholder_and_attaches(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        f = tmp_path / "x.txt"
        f.write_text("x")
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T1:channel:C1:thread:1.1",
                "--edit", "1.2", "--file", str(f), "done - attached",
            ])
        assert result.exit_code == 0, result.output
        body = json.loads(reqs[0].content)
        assert body["edit_ref"] == "1.2"
        assert body["files"][0]["name"] == "x.txt"
        assert body["text"] == "done - attached"

    def test_edit_with_file_requires_text(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        f = tmp_path / "x.txt"
        f.write_text("x")
        runner = CliRunner()
        result = runner.invoke(main, [
            "reply", "slack:T1:dm:D1", "--edit", "1.2", "--file", str(f),
        ], input="")
        assert result.exit_code == 1
        assert "requires TEXT" in result.output

    def test_reads_text_from_stdin(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(
                main, ["reply", "slack:T1:dm:D1"], input="piped text\n")
        assert result.exit_code == 0, result.output
        assert json.loads(reqs[0].content)["text"] == "piped text"

    def test_piped_comment_survives_with_file(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"pdf")
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["reply", "slack:T1:dm:D1", "--file", str(f)],
                input="please review this\n")
        assert result.exit_code == 0, result.output
        body = json.loads(reqs[0].content)
        assert body["text"] == "please review this"
        assert body["files"][0]["name"] == "report.pdf"

    def test_unescapes_literal_newlines(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "reply", "slack:T1:dm:D1", "line one\\nline two\\tindented",
            ])
        assert result.exit_code == 0, result.output
        assert json.loads(reqs[0].content)["text"] == "line one\nline two\tindented"

    def test_rejects_invalid_ref(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "slack:T1:D1", "hi"])
        assert result.exit_code == 1
        assert "Invalid conversation reference" in result.output

    def test_unsupported_channel_error_comes_from_gateway(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request):
            return httpx.Response(
                400, json={"error": "unsupported channel: telegram"})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(
                main, ["reply", "telegram:12345:dm:67890", "hi"])
        assert result.exit_code == 1
        assert "unsupported channel: telegram" in result.output

    def test_rejects_empty_text(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "slack:T1:dm:D1"], input="  \n")
        assert result.exit_code == 1
        assert "No text to send" in result.output

    def test_missing_bubble_credential(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch, with_bubble=False)
        runner = CliRunner()
        result = runner.invoke(main, ["reply", "slack:T1:dm:D1", "hi"])
        assert result.exit_code == 1
        assert "bubble" in result.output.lower()

    def test_gateway_error_is_surfaced_with_hint(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request):
            return httpx.Response(400, json={"error": "no send credential registered for slack:T1"})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, ["reply", "slack:T1:dm:D1", "hi"])
        assert result.exit_code == 1
        assert "no send credential registered for slack:T1" in result.output
        assert "channel gateway" in result.output

    def test_non_object_gateway_json_is_surfaced_as_gateway_error(
            self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request):
            return httpx.Response(200, json=[{"ok": True}])

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, ["reply", "slack:T1:dm:D1", "hi"])
        assert result.exit_code == 1
        assert "JSON response" in result.output
        assert "channel gateway" in result.output

    def test_plain_text_gateway_error_is_surfaced(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request):
            return httpx.Response(502, text="bad gateway")

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, ["reply", "slack:T1:dm:D1", "hi"])
        assert result.exit_code == 1
        assert "bad gateway" in result.output
        assert "channel gateway" in result.output

    def test_server_unreachable(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request):
            raise httpx.ConnectError("connection refused")

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, ["reply", "slack:T1:dm:D1", "hi"])
        assert result.exit_code == 1
        assert "unreachable" in result.output


class TestReadConversationCommand:
    def _history_handler(self, requests_log, messages):
        def handler(request: httpx.Request) -> httpx.Response:
            requests_log.append(request)
            return httpx.Response(200, json={"ok": True, "messages": messages})
        return handler

    def test_renders_messages(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        messages = [
            {"user": "U1", "text": "question", "ts": "12.34"},
            {"user": "U2", "text": "answer", "ts": "12.35",
             "files": [{"name": "a.png", "mimetype": "image/png"}]},
        ]
        with patch.object(pooled, '_client',
                          _mock_client(self._history_handler(reqs, messages))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "read-conversation", "slack:T1:channel:C1:thread:12.34", "-n", "50",
            ])
        assert result.exit_code == 0, result.output
        assert "[12.34] U1: question" in result.output
        assert ">> a.png (image/png)" in result.output
        assert "2 message(s)" in result.output

        req = reqs[0]
        assert req.method == "GET"
        assert "/channels/history?" in str(req.url)
        assert "limit=50" in str(req.url)
        assert req.headers["x-moda-signature"]

    def test_json_output(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        messages = [{"user": "U1", "text": "hi", "ts": "1.2"}]
        with patch.object(pooled, '_client',
                          _mock_client(self._history_handler([], messages))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "read-conversation", "slack:T1:dm:D1:thread:1.2", "--json-output",
            ])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == messages


class TestDeprecatedSlackShims:
    """slack-reply / slack-upload-file / slack-read-thread rewrite their
    flags into a conversation reference and call the gateway path."""

    def test_slack_reply_rewrites_flags_and_warns(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T0952RZRZ0X", "-c", "C123",
                "-t", "171.42", "hello",
            ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        body = json.loads(reqs[0].content)
        assert body["conversation"] == "slack:T0952RZRZ0X:channel:C123:thread:171.42"
        assert body["text"] == "hello"
        assert body["mode"] == "final"

    def test_slack_reply_dm_channel_without_thread(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T1", "-c", "D456", "hi",
            ])
        assert result.exit_code == 0, result.output
        body = json.loads(reqs[0].content)
        assert body["conversation"] == "slack:T1:dm:D456"

    def test_slack_reply_edit_flag(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-reply", "-w", "T1", "-c", "C1", "-t", "171.42",
                "--edit", "171.99", "final answer",
            ])
        assert result.exit_code == 0, result.output
        body = json.loads(reqs[0].content)
        assert body["edit_ref"] == "171.99"

    def test_slack_upload_file_shim(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        f = tmp_path / "shot.png"
        f.write_bytes(b"png-bytes")
        reqs = []
        with patch.object(pooled, '_client', _mock_client(_gateway_ok(reqs))):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-upload-file", str(f), "-w", "T1", "-c", "C1",
                "-t", "171.42", "--title", "Shot", "--comment", "see this",
            ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        assert "Uploaded shot.png to C1" in result.output
        body = json.loads(reqs[0].content)
        assert body["conversation"] == "slack:T1:channel:C1:thread:171.42"
        assert body["text"] == "see this"
        assert body["files"][0]["name"] == "shot.png"
        assert body["files"][0]["title"] == "Shot"
        assert base64.b64decode(body["files"][0]["content_b64"]) == b"png-bytes"

    def test_slack_read_thread_shim(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            return httpx.Response(200, json={"ok": True, "messages": [
                {"user": "U1", "text": "hi", "ts": "1.2"},
            ]})

        with patch.object(pooled, '_client', _mock_client(handler)):
            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-read-thread", "-w", "T1", "-c", "C1", "-t", "1.2",
            ])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        assert "[1.2] U1: hi" in result.output
        assert "/channels/history?" in str(reqs[0].url)
        assert "slack%3AT1%3Achannel%3AC1%3Athread%3A1.2" in str(reqs[0].url)
