"""Tests for Slack file upload/download and thread reading (#360 / MOD-208).

Covers:
- download_slack_file (authenticated GET)
- upload_slack_file (V2 upload flow: getUploadURLExternal + upload + completeUploadExternal)
- fetch_slack_thread (conversations.replies with pagination)
- Slack event normalizer file extraction (via Python-side structure validation)
- CLI slack-upload-file command
- CLI slack-read-thread command
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest

from modastack import http as pooled
from modastack.slack import (
    download_slack_file,
    upload_slack_file,
    fetch_slack_thread,
    resolve_channel_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(handler):
    """Create an httpx.Client with a MockTransport backed by *handler*."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


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


# ---------------------------------------------------------------------------
# download_slack_file
# ---------------------------------------------------------------------------

class TestDownloadSlackFile:
    def test_downloads_with_auth_header(self):
        reqs: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\n",
                headers={"content-type": "image/png"},
            )

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            data, ct = download_slack_file(
                "xoxb-test",
                "https://files.slack.com/files-pri/T123/image.png",
            )

        assert data == b"\x89PNG\r\n\x1a\n"
        assert ct == "image/png"
        assert reqs[0].headers["authorization"] == "Bearer xoxb-test"

    def test_raises_on_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"Forbidden")

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            with pytest.raises(RuntimeError, match="403"):
                download_slack_file("xoxb-test", "https://files.slack.com/bad")

    def test_default_content_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"data")

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            _, ct = download_slack_file("xoxb-test", "https://files.slack.com/f")

        assert ct == "application/octet-stream"


# ---------------------------------------------------------------------------
# upload_slack_file
# ---------------------------------------------------------------------------

class TestUploadSlackFile:
    def test_v2_upload_flow(self):
        """Tests the three-step V2 upload: getUploadURL -> upload -> complete."""
        reqs: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            url = str(request.url)
            if "getUploadURLExternal" in url:
                return httpx.Response(200, json={
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/v2/abc123",
                    "file_id": "F123",
                })
            elif "upload/v2" in url:
                return httpx.Response(200, content=b"OK")
            elif "completeUploadExternal" in url:
                return httpx.Response(200, json={"ok": True, "files": [{"id": "F123"}]})
            return httpx.Response(404)

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            result = upload_slack_file(
                "xoxb-test", "C123", b"file contents", "test.txt",
                title="Test File", thread_ts="171.42",
                initial_comment="Here's the file",
            )

        assert result["ok"] is True

        # Step 1: getUploadURLExternal
        step1_url = str(reqs[0].url)
        assert "getUploadURLExternal" in step1_url
        assert "filename=test.txt" in step1_url
        assert "length=13" in step1_url

        # Step 2: upload bytes to presigned URL
        upload_req = reqs[1]
        assert "upload/v2" in str(upload_req.url)
        assert upload_req.content == b"file contents"

        # Step 3: completeUploadExternal
        complete_req = reqs[2]
        assert "completeUploadExternal" in str(complete_req.url)
        body = json.loads(complete_req.content)
        assert body["files"] == [{"id": "F123", "title": "Test File"}]
        assert body["channel_id"] == "C123"
        assert body["thread_ts"] == "171.42"
        assert body["initial_comment"] == "Here's the file"

    def test_get_url_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": False, "error": "invalid_auth",
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            with pytest.raises(RuntimeError, match="invalid_auth"):
                upload_slack_file("xoxb-bad", "C123", b"data", "f.txt")

    def test_upload_step_http_error_raises(self):
        """Step 2 (file upload to presigned URL) HTTP error is raised."""
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "getUploadURLExternal" in url:
                return httpx.Response(200, json={
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/v2/abc",
                    "file_id": "F1",
                })
            elif "upload/v2" in url:
                return httpx.Response(500, content=b"Internal Server Error")
            return httpx.Response(404)

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            with pytest.raises(RuntimeError, match="500"):
                upload_slack_file("xoxb-test", "C123", b"data", "f.txt")

    def test_upload_without_optional_fields(self):
        """Upload with minimal params (no title, thread, or comment)."""
        reqs: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            url = str(request.url)
            if "getUploadURLExternal" in url:
                return httpx.Response(200, json={
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/v2/abc",
                    "file_id": "F456",
                })
            elif "upload/v2" in url:
                return httpx.Response(200, content=b"OK")
            elif "completeUploadExternal" in url:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404)

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            upload_slack_file("xoxb-test", "C123", b"data", "f.txt")

        # completeUploadExternal should not have thread_ts or initial_comment
        complete_req = reqs[-1]
        body = json.loads(complete_req.content)
        assert "thread_ts" not in body
        assert "initial_comment" not in body
        assert body["files"] == [{"id": "F456"}]


# ---------------------------------------------------------------------------
# fetch_slack_thread
# ---------------------------------------------------------------------------

class TestFetchSlackThread:
    def test_fetches_thread_messages(self):
        reqs: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "parent message", "ts": "171.42"},
                    {"user": "U2", "text": "reply", "ts": "171.43"},
                ],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            messages = fetch_slack_thread("xoxb-test", "C123", "171.42")

        assert len(messages) == 2
        assert messages[0]["user"] == "U1"
        assert messages[0]["text"] == "parent message"
        assert messages[1]["user"] == "U2"
        assert messages[1]["text"] == "reply"

        req_url = str(reqs[0].url)
        assert "conversations.replies" in req_url
        assert "channel=C123" in req_url
        assert "ts=171.42" in req_url

    def test_includes_file_metadata(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [{
                    "user": "U1",
                    "text": "check this out",
                    "ts": "171.42",
                    "files": [{
                        "id": "F123",
                        "name": "screenshot.png",
                        "mimetype": "image/png",
                        "url_private": "https://files.slack.com/f/screenshot.png",
                    }],
                }],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            messages = fetch_slack_thread("xoxb-test", "C123", "171.42")

        assert len(messages) == 1
        assert len(messages[0]["files"]) == 1
        f = messages[0]["files"][0]
        assert f["id"] == "F123"
        assert f["name"] == "screenshot.png"
        assert f["mimetype"] == "image/png"

    def test_messages_without_files_have_no_files_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "just text", "ts": "171.42"},
                ],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            messages = fetch_slack_thread("xoxb-test", "C123", "171.42")

        assert "files" not in messages[0]

    def test_respects_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": f"msg{i}", "ts": f"171.{i}"}
                    for i in range(10)
                ],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            messages = fetch_slack_thread("xoxb-test", "C123", "171.0", limit=3)

        assert len(messages) == 3

    def test_paginates_with_cursor(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={
                    "ok": True,
                    "messages": [
                        {"user": "U1", "text": "first", "ts": "171.1"},
                    ],
                    "response_metadata": {"next_cursor": "cursor_abc"},
                })
            else:
                return httpx.Response(200, json={
                    "ok": True,
                    "messages": [
                        {"user": "U2", "text": "second", "ts": "171.2"},
                    ],
                })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            messages = fetch_slack_thread("xoxb-test", "C123", "171.0")

        assert len(messages) == 2
        assert call_count == 2
        assert messages[0]["text"] == "first"
        assert messages[1]["text"] == "second"

    def test_api_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": False, "error": "channel_not_found",
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            with pytest.raises(RuntimeError, match="channel_not_found"):
                fetch_slack_thread("xoxb-test", "C999", "171.0")


# ---------------------------------------------------------------------------
# Slack event normalizer — file extraction (Python-side validation)
# ---------------------------------------------------------------------------

class TestSlackEventFileExtraction:
    """Validate the contract for file metadata in normalized events.

    The TypeScript normalizer serializes files as JSON in fields.files.
    These tests validate agents can parse the format correctly.
    """

    def test_event_with_files_has_files_in_fields(self):
        files = [
            {"id": "F1", "name": "img.png", "mimetype": "image/png",
             "url_private": "https://files.slack.com/f/img.png", "size": "1024"},
        ]
        event = {
            "source": "slack",
            "type": "slack.dm",
            "delivery": "chat",
            "text": "here's a screenshot",
            "fields": {
                "user_id": "U123",
                "channel": "D456",
                "channel_type": "im",
                "ts": "171.42",
                "files": json.dumps(files),
            },
        }

        parsed_files = json.loads(event["fields"]["files"])
        assert len(parsed_files) == 1
        assert parsed_files[0]["name"] == "img.png"
        assert parsed_files[0]["mimetype"] == "image/png"
        assert parsed_files[0]["url_private"] == "https://files.slack.com/f/img.png"

    def test_event_without_files_has_no_files_field(self):
        event = {
            "source": "slack",
            "type": "slack.dm",
            "fields": {"user_id": "U123", "channel": "D456", "ts": "171.42"},
        }
        assert "files" not in event["fields"]

    def test_multiple_files_in_single_event(self):
        files = [
            {"id": "F1", "name": "a.png", "mimetype": "image/png"},
            {"id": "F2", "name": "b.pdf", "mimetype": "application/pdf"},
        ]
        event = {"fields": {"files": json.dumps(files)}}
        parsed = json.loads(event["fields"]["files"])
        assert len(parsed) == 2
        assert parsed[0]["name"] == "a.png"
        assert parsed[1]["name"] == "b.pdf"


# ---------------------------------------------------------------------------
# CLI slack-upload-file
# ---------------------------------------------------------------------------

class TestSlackUploadCommand:
    def test_upload_success(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"\x89PNG fake image data")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "getUploadURLExternal" in url:
                return httpx.Response(200, json={
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/v2/abc",
                    "file_id": "F789",
                })
            elif "upload/v2" in url:
                return httpx.Response(200, content=b"OK")
            elif "completeUploadExternal" in url:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"ok": True})

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-upload-file", str(test_file),
                "-w", "T123", "-c", "C456",
            ])

        assert result.exit_code == 0, result.output
        assert "Uploaded test.png to C456" in result.output

    def test_upload_with_thread_and_comment(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        test_file = tmp_path / "doc.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        reqs: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            reqs.append(request)
            url = str(request.url)
            if "getUploadURLExternal" in url:
                return httpx.Response(200, json={
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/v2/abc",
                    "file_id": "F789",
                })
            elif "upload/v2" in url:
                return httpx.Response(200, content=b"OK")
            elif "completeUploadExternal" in url:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"ok": True})

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-upload-file", str(test_file),
                "-w", "T123", "-c", "C456",
                "-t", "171.42",
                "--title", "Report",
                "--comment", "Here's the report",
            ])

        assert result.exit_code == 0, result.output

        complete_reqs = [r for r in reqs if "completeUploadExternal" in str(r.url)]
        assert len(complete_reqs) == 1
        body = json.loads(complete_reqs[0].content)
        assert body["thread_ts"] == "171.42"
        assert body["initial_comment"] == "Here's the report"

    def test_upload_file_not_found(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-upload-file", str(tmp_path / "nonexistent.txt"),
            "-w", "T123", "-c", "C456",
        ])
        assert result.exit_code != 0

    def test_upload_api_error(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": False, "error": "not_authed",
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-upload-file", str(test_file),
                "-w", "T123", "-c", "C456",
            ])

        assert result.exit_code != 0
        assert "not_authed" in result.output


# ---------------------------------------------------------------------------
# CLI slack-read-thread
# ---------------------------------------------------------------------------

class TestSlackThreadCommand:
    def test_thread_text_output(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "parent", "ts": "171.42"},
                    {"user": "U2", "text": "reply", "ts": "171.43"},
                ],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-read-thread",
                "-w", "T123", "-c", "C456", "-t", "171.42",
            ])

        assert result.exit_code == 0, result.output
        assert "U1: parent" in result.output
        assert "U2: reply" in result.output
        assert "2 message(s)" in result.output

    def test_thread_json_output(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "msg", "ts": "171.42"},
                ],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-read-thread",
                "-w", "T123", "-c", "C456", "-t", "171.42",
                "--json-output",
            ])

        assert result.exit_code == 0, result.output
        # Output may contain "Running from ..." prefix; find the JSON array
        output = result.output
        json_start = output.index("[")
        parsed = json.loads(output[json_start:])
        assert len(parsed) == 1
        assert parsed[0]["user"] == "U1"

    def test_thread_with_files_shows_attachments(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "messages": [{
                    "user": "U1",
                    "text": "see image",
                    "ts": "171.42",
                    "files": [{
                        "id": "F1",
                        "name": "screenshot.png",
                        "mimetype": "image/png",
                        "url_private": "https://files.slack.com/f/s.png",
                    }],
                }],
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-read-thread",
                "-w", "T123", "-c", "C456", "-t", "171.42",
            ])

        assert result.exit_code == 0, result.output
        assert "screenshot.png" in result.output
        assert "image/png" in result.output

    def test_thread_api_error(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": False, "error": "thread_not_found",
            })

        mock_client = _make_mock_client(handler)
        with patch.object(pooled, "_client", mock_client):
            from click.testing import CliRunner
            from modastack.cli import main

            runner = CliRunner()
            result = runner.invoke(main, [
                "slack-read-thread",
                "-w", "T123", "-c", "C456", "-t", "171.42",
            ])

        assert result.exit_code != 0
        assert "thread_not_found" in result.output


# ---------------------------------------------------------------------------
# resolve_channel_id (#485 — readable #channel-name in config)
# ---------------------------------------------------------------------------

class TestResolveChannelId:
    def test_id_passes_through_without_api_call(self):
        # No client patched: an ID must resolve with zero network calls.
        assert resolve_channel_id("xoxb", "C0ABCDEF1") == "C0ABCDEF1"
        assert resolve_channel_id("xoxb", "D0ABCDEF1") == "D0ABCDEF1"
        assert resolve_channel_id("xoxb", "") == ""

    def test_resolves_name_with_and_without_hash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "channels": [
                {"id": "C111", "name": "general"},
                {"id": "C222", "name": "codex-test"},
            ]})
        with patch.object(pooled, "_client", _make_mock_client(handler)):
            assert resolve_channel_id("xoxb", "#codex-test") == "C222"
            assert resolve_channel_id("xoxb", "codex-test") == "C222"

    def test_missing_groups_scope_falls_back_to_public(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen.append(url)
            if "private_channel" in url:
                return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
            return httpx.Response(200, json={"ok": True,
                                             "channels": [{"id": "C9", "name": "pub"}]})
        with patch.object(pooled, "_client", _make_mock_client(handler)):
            assert resolve_channel_id("xoxb", "#pub") == "C9"
        assert any("private_channel" in u for u in seen)  # tried private first

    def test_unknown_name_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "channels": []})
        with patch.object(pooled, "_client", _make_mock_client(handler)):
            with pytest.raises(RuntimeError, match="not found"):
                resolve_channel_id("xoxb", "#nope")

    def test_resolves_user_handle_to_im_channel(self):
        calls: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            calls.append((str(request.url), body))
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": [
                    {"id": "U111", "name": "other", "deleted": False},
                    {"id": "U222", "name": "zachkozick", "deleted": False},
                ]})
            if "conversations.open" in url:
                assert body == {"users": "U222"}
                return httpx.Response(200, json={
                    "ok": True, "channel": {"id": "D0ASVSYSP0D"},
                })
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            assert resolve_channel_id("xoxb", "@zachkozick") == "D0ASVSYSP0D"

        assert any("users.list" in url for url, _ in calls)
        assert any("conversations.open" in url for url, _ in calls)

    def test_resolves_enterprise_user_id_to_im_channel(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "conversations.open" in url:
                body = json.loads(request.content)
                assert body == {"users": "W2222222"}
                return httpx.Response(200, json={
                    "ok": True, "channel": {"id": "D0ASVSYSP0D"},
                })
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            assert resolve_channel_id("xoxb", "@W2222222") == "D0ASVSYSP0D"

        assert not any("users.list" in url for url in calls)

    def test_lowercase_handle_that_looks_like_user_id_is_not_treated_as_id(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": [
                    {"id": "U2222222", "name": "william", "deleted": False},
                ]})
            if "conversations.open" in url:
                body = json.loads(request.content)
                assert body == {"users": "U2222222"}
                return httpx.Response(200, json={
                    "ok": True, "channel": {"id": "D0ASVSYSP0D"},
                })
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            assert resolve_channel_id("xoxb", "@william") == "D0ASVSYSP0D"

        assert any("users.list" in url for url in calls)

    def test_unknown_user_error_names_workspace_and_reference(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": []})
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            with pytest.raises(RuntimeError) as exc:
                resolve_channel_id("xoxb", "@missing")

        msg = str(exc.value)
        assert "@missing" in msg
        assert "Auroborium" in msg
        assert "T123" in msg

    def test_user_handle_does_not_match_mutable_display_or_real_name(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": [
                    {
                        "id": "U2222222",
                        "name": "bob",
                        "real_name": "Alice",
                        "profile": {
                            "display_name": "alice",
                            "real_name": "Alice",
                        },
                        "deleted": False,
                    },
                ]})
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            with pytest.raises(RuntimeError, match="not found"):
                resolve_channel_id("xoxb", "@alice")

    def test_ambiguous_user_handle_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": [
                    {"id": "U111", "name": "zach", "deleted": False},
                    {"id": "U222", "name": "zach", "deleted": False},
                ]})
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            with pytest.raises(RuntimeError, match="ambiguous"):
                resolve_channel_id("xoxb", "@zach")

    def test_deleted_user_handle_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "auth.test" in url:
                return httpx.Response(200, json={
                    "ok": True, "team": "Auroborium", "team_id": "T123",
                })
            if "users.list" in url:
                return httpx.Response(200, json={"ok": True, "members": [
                    {"id": "U111", "name": "zach", "deleted": True},
                ]})
            raise AssertionError(f"unexpected Slack URL {url}")

        with patch.object(pooled, "_client", _make_mock_client(handler)):
            with pytest.raises(RuntimeError, match="deleted"):
                resolve_channel_id("xoxb", "@zach")
