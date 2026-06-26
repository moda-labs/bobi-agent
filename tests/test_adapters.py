"""Tests for the ingestion adapter registry."""

from pathlib import Path
from unittest.mock import patch

import httpx

from bobi.config import Config, ServiceConfig
from bobi.events.adapters import (
    is_registered,
    detect,
    _parse_github_url,
    _is_channel_id,
    _resolve_channel_names,
)
from bobi import http as pooled


def _mock_httpx_response(json_data):
    """Create a mock httpx.Response for testing."""
    return httpx.Response(200, json=json_data)


class TestAdapterRegistry:

    def test_builtin_adapters_registered(self):
        assert is_registered("github")
        assert is_registered("slack")
        assert is_registered("linear")

    def test_unknown_service_not_registered(self):
        assert not is_registered("email")
        assert not is_registered("discord")

    def test_detect_unregistered_falls_back_to_name(self):
        cfg = Config()
        keys = detect("email", Path("/fake"), cfg)
        assert keys == ["email"]

    def test_detect_unregistered_custom_service(self):
        cfg = Config()
        keys = detect("my-custom-source", Path("/fake"), cfg)
        assert keys == ["my-custom-source"]


class TestGithubDetector:

    def test_parse_https_url(self):
        assert _parse_github_url("https://github.com/moda-labs/modastack.git") == "moda-labs/modastack"

    def test_parse_ssh_url(self):
        assert _parse_github_url("git@github.com:moda-labs/modastack.git") == "moda-labs/modastack"

    def test_parse_non_github_url(self):
        assert _parse_github_url("https://gitlab.com/foo/bar") == ""

    def test_detect_from_git_remote(self, tmp_path):
        """Auto-detect github:org/repo when project is a git repo."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test-org/test-repo.git"],
            capture_output=True, cwd=str(tmp_path),
        )

        cfg = Config()
        keys = detect("github", tmp_path, cfg)
        assert keys == ["github:test-org/test-repo"]


class TestSlackDetector:

    def test_detect_with_token(self, tmp_path):
        responses = [_mock_httpx_response({"ok": True, "team_id": "T123ABC"})]
        call_idx = iter(range(len(responses)))

        def _handler(request):
            return responses[next(call_idx)]

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"}),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == ["slack:T123ABC"]

    def test_detect_with_token_uses_app_qualified_topic(self, tmp_path):
        responses = [
            _mock_httpx_response({"ok": True, "team_id": "T123ABC", "bot_id": "B123"}),
            _mock_httpx_response({"ok": True, "bot": {"app_id": "A123"}}),
        ]
        call_idx = iter(range(len(responses)))

        def _handler(request):
            assert ("auth.test" in str(request.url)
                    or "bots.info?bot=B123" in str(request.url))
            return responses[next(call_idx)]

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"}),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == ["slack:T123ABC:app:A123"]

    def test_detect_without_token(self, tmp_path):
        cfg = Config(services=[ServiceConfig(name="slack")])
        keys = detect("slack", tmp_path, cfg)
        assert keys == []

    def test_detect_scopes_to_configured_channels(self, tmp_path):
        responses = [_mock_httpx_response({"ok": True, "team_id": "T123ABC"})]
        call_idx = iter(range(len(responses)))

        def _handler(request):
            return responses[next(call_idx)]

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                              channels=["C0SUPPORT", "C0ALERTS"]),
            ])
            keys = detect("slack", tmp_path, cfg)
        # per-channel keys, not the whole workspace
        assert keys == ["slack:T123ABC:C0SUPPORT", "slack:T123ABC:C0ALERTS"]

    def test_detect_scopes_configured_channels_to_app(self, tmp_path):
        responses = [
            _mock_httpx_response({"ok": True, "team_id": "T123ABC", "bot_id": "B123"}),
            _mock_httpx_response({"ok": True, "bot": {"app_id": "A123"}}),
        ]
        call_idx = iter(range(len(responses)))

        def _handler(request):
            return responses[next(call_idx)]

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                              channels=["C0SUPPORT", "C0ALERTS"]),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == [
            "slack:T123ABC:app:A123:C0SUPPORT",
            "slack:T123ABC:app:A123:C0ALERTS",
        ]

    def test_detect_resolves_channel_names_to_ids(self, tmp_path):
        """End-to-end: human-readable channel names get resolved via Slack API."""
        # First call: auth.test → team_id.  Second call: conversations.list → name→ID map.
        responses = [
            _mock_httpx_response({"ok": True, "team_id": "T123ABC"}),
            _mock_httpx_response({
                "ok": True,
                "channels": [
                    {"id": "C_SUPPORT", "name": "support"},
                    {"id": "C_GENERAL", "name": "general"},
                ],
                "response_metadata": {"next_cursor": ""},
            }),
        ]
        call_idx = iter(range(len(responses)))

        def _handler(request):
            return responses[next(call_idx)]

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                              channels=["#support", "general"]),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == ["slack:T123ABC:C_SUPPORT", "slack:T123ABC:C_GENERAL"]


class TestChannelNameResolution:

    def test_is_channel_id_recognises_ids(self):
        assert _is_channel_id("C0ABC123") is True
        assert _is_channel_id("G0ABC123") is True

    def test_is_channel_id_rejects_names(self):
        assert _is_channel_id("support") is False
        assert _is_channel_id("#support") is False
        assert _is_channel_id("") is False

    def test_resolve_passes_ids_through(self):
        # IDs pass through without any HTTP call
        transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("should not be called")))
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = _resolve_channel_names("xoxb-test", ["C0AAA", "C0BBB"])
        assert result == ["C0AAA", "C0BBB"]

    def test_resolve_looks_up_names(self):
        transport = httpx.MockTransport(lambda r: _mock_httpx_response({
            "ok": True,
            "channels": [
                {"id": "C_SUPPORT", "name": "support"},
                {"id": "C_ALERTS", "name": "alerts"},
            ],
            "response_metadata": {"next_cursor": ""},
        }))
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = _resolve_channel_names("xoxb-test", ["#support", "alerts"])
        assert result == ["C_SUPPORT", "C_ALERTS"]

    def test_resolve_mixed_ids_and_names(self):
        transport = httpx.MockTransport(lambda r: _mock_httpx_response({
            "ok": True,
            "channels": [{"id": "C_SUPPORT", "name": "support"}],
            "response_metadata": {"next_cursor": ""},
        }))
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = _resolve_channel_names("xoxb-test", ["C0AAA", "#support"])
        assert result == ["C0AAA", "C_SUPPORT"]

    def test_resolve_drops_unresolvable_names(self):
        transport = httpx.MockTransport(lambda r: _mock_httpx_response({
            "ok": True,
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }))
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = _resolve_channel_names("xoxb-test", ["#nonexistent"])
        assert result == []


def test_slack_keys_helper():
    from bobi.events.adapters import _slack_keys
    assert _slack_keys("T1", []) == ["slack:T1"]
    assert _slack_keys("T1", ["C1", "C2"]) == ["slack:T1:C1", "slack:T1:C2"]
    assert _slack_keys("T1", [], "A1") == ["slack:T1:app:A1"]
    assert _slack_keys("T1", ["C1"], "A1") == ["slack:T1:app:A1:C1"]
    assert _slack_keys("", ["C1"]) == []


class TestLinearDetector:

    def test_detect_with_key(self, tmp_path):
        transport = httpx.MockTransport(lambda r: _mock_httpx_response({
            "data": {"teams": {"nodes": [{"key": "ENG"}, {"key": "OPS"}]}}
        }))
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="linear", credentials={"api_key": "lin_test"}),
            ])
            keys = detect("linear", tmp_path, cfg)
        assert keys == ["linear:ENG", "linear:OPS"]

    def test_detect_without_key(self, tmp_path):
        cfg = Config(services=[ServiceConfig(name="linear")])
        keys = detect("linear", tmp_path, cfg)
        assert keys == []
