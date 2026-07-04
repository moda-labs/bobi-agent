"""Tests for Slack file metadata contracts and channel resolution.

Covers:
- Slack event normalizer file extraction (via Python-side structure validation)
- resolve_channel_id (setup-time channel/user reference resolution)

File upload, thread reading, and the slack-* CLI commands moved to the
channel gateway in #190 Phase 2 - their coverage lives in
event-server/test/ (adapter) and tests/test_slack_reply.py (CLI shims).
"""

import json
from unittest.mock import patch

import httpx
import pytest

from bobi import http as pooled
from bobi.slack import resolve_channel_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(handler):
    """Create an httpx.Client with a MockTransport backed by *handler*."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


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
