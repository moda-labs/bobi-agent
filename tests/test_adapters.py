"""Tests for the ingestion adapter registry."""

from pathlib import Path
from unittest.mock import patch

import httpx

from bobi.config import Config, ServiceConfig
from bobi.events.adapters import (
    is_registered,
    detect,
    _parse_github_url,
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
        assert is_registered("discord")

    def test_unknown_service_not_registered(self):
        assert not is_registered("email")

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
        assert _parse_github_url("https://github.com/moda-labs/bobi-agent.git") == "moda-labs/bobi-agent"

    def test_parse_ssh_url(self):
        assert _parse_github_url("git@github.com:moda-labs/bobi-agent.git") == "moda-labs/bobi-agent"

    def test_parse_non_github_url(self):
        assert _parse_github_url("https://gitlab.com/foo/bar") == ""

    def test_enterprise_host_is_not_github_com(self):
        """D075 — a substring match makes 'github.company.com' look like
        github.com plus a path.

        'github.com' matches the host's first ten characters, so splitting on
        it yields 'pany.com/acme/widget' → slug 'pany.com/acme' and a
        subscription topic ('github:pany.com/acme') no event can ever match.
        Grant authorization for it then fails against the real GitHub API and
        drops the topic with a misleading credential warning. The right answer
        for a host bobi cannot talk to is no slug at all.
        """
        assert _parse_github_url("https://github.company.com/acme/widget.git") == ""
        assert _parse_github_url("git@github.company.com:acme/widget.git") == ""

    def test_host_suffix_is_not_github_com(self):
        # An attacker-registerable lookalike must not resolve either.
        assert _parse_github_url("https://notgithub.com/acme/widget") == ""
        assert _parse_github_url("https://github.com.evil.test/acme/widget") == ""

    def test_parse_ssh_scheme_url(self):
        assert _parse_github_url(
            "ssh://git@github.com/moda-labs/bobi-agent.git") == "moda-labs/bobi-agent"

    def test_parse_url_without_credentials_or_suffix(self):
        assert _parse_github_url("https://github.com/acme/widget") == "acme/widget"

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

    def test_detect_without_app_identity_disables_subscription(self, tmp_path, caplog):
        responses = [_mock_httpx_response({"ok": True, "team_id": "T123ABC",
                                           "bot_id": "B123"})]
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
        assert keys == []
        assert "users:read" in caplog.text

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
        # auth.test → team_id; then one conversations.list lookup per name.
        def _handler(request):
            if "auth.test" in str(request.url):
                return _mock_httpx_response({"ok": True, "team_id": "T123ABC",
                                             "bot_id": "B123"})
            if "bots.info" in str(request.url):
                return _mock_httpx_response({"ok": True, "bot": {"app_id": "A123"}})
            return _mock_httpx_response({
                "ok": True,
                "channels": [
                    {"id": "C_SUPPORT", "name": "support"},
                    {"id": "C_GENERAL", "name": "general"},
                ],
                "response_metadata": {"next_cursor": ""},
            })

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                              channels=["#support", "general"]),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == [
            "slack:T123ABC:app:A123:C_SUPPORT",
            "slack:T123ABC:app:A123:C_GENERAL",
        ]

    def test_detect_drops_all_subscriptions_when_lookup_transport_fails(self, tmp_path):
        """A transport failure mid-resolution must not widen the subscription.

        Configured channels scope the topic to those channels. If a hiccup
        during resolution were swallowed, the channel list would come back
        empty and ``_slack_keys`` would subscribe to the WHOLE workspace —
        strictly more traffic than the operator asked for. Detection yields
        nothing instead.
        """
        def _handler(request):
            if "auth.test" in str(request.url):
                return _mock_httpx_response({"ok": True, "team_id": "T123ABC"})
            raise httpx.ConnectError("slack unreachable")

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            cfg = Config(services=[
                ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                              channels=["#support"]),
            ])
            keys = detect("slack", tmp_path, cfg)
        assert keys == []


class TestChannelNameResolution:
    """Subscription-side channel resolution.

    The ID-vs-name decision itself belongs to ``bobi.slack.resolve_channel_id``
    and is covered by ``test_slack_files_and_threads.py``; what is pinned here
    is the policy this module adds on top — drop what cannot be resolved,
    but let a transport failure through.
    """

    def test_resolve_passes_ids_through(self):
        # IDs pass through without any HTTP call
        transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("should not be called")))
        mock_client = httpx.Client(transport=transport)
        with patch.object(pooled, '_client', mock_client):
            result = _resolve_channel_names("xoxb-test", ["C0AAAAAA", "C0BBBBBB"])
        assert result == ["C0AAAAAA", "C0BBBBBB"]

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
            result = _resolve_channel_names("xoxb-test", ["C0AAAAAA", "#support"])
        assert result == ["C0AAAAAA", "C_SUPPORT"]

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
