"""Tests for the ingestion adapter registry."""

from pathlib import Path
from unittest.mock import patch

from modastack.config import Config, ServiceConfig
from modastack.events.adapters import (
    is_registered,
    detect,
    _parse_github_url,
    _is_channel_id,
    _resolve_channel_names,
)


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

    @patch("urllib.request.urlopen")
    def test_detect_with_token(self, mock_urlopen, tmp_path):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({"ok": True, "team_id": "T123ABC"}).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        cfg = Config(services=[
            ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"}),
        ])
        keys = detect("slack", tmp_path, cfg)
        assert keys == ["slack:T123ABC"]

    def test_detect_without_token(self, tmp_path):
        cfg = Config(services=[ServiceConfig(name="slack")])
        keys = detect("slack", tmp_path, cfg)
        assert keys == []

    @patch("urllib.request.urlopen")
    def test_detect_scopes_to_configured_channels(self, mock_urlopen, tmp_path):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({"ok": True, "team_id": "T123ABC"}).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        cfg = Config(services=[
            ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"},
                          channels=["C0SUPPORT", "C0ALERTS"]),
        ])
        keys = detect("slack", tmp_path, cfg)
        # per-channel keys, not the whole workspace
        assert keys == ["slack:T123ABC:C0SUPPORT", "slack:T123ABC:C0ALERTS"]


    @patch("urllib.request.urlopen")
    def test_detect_resolves_channel_names_to_ids(self, mock_urlopen, tmp_path):
        """End-to-end: human-readable channel names get resolved via Slack API."""
        import json

        # First call: auth.test → team_id.  Second call: conversations.list → name→ID map.
        auth_resp = type("Resp", (), {
            "read": lambda self: json.dumps({"ok": True, "team_id": "T123ABC"}).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        conv_resp = type("Resp", (), {
            "read": lambda self: json.dumps({
                "ok": True,
                "channels": [
                    {"id": "C_SUPPORT", "name": "support"},
                    {"id": "C_GENERAL", "name": "general"},
                ],
                "response_metadata": {"next_cursor": ""},
            }).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.side_effect = [auth_resp, conv_resp]

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

    @patch("urllib.request.urlopen")
    def test_resolve_passes_ids_through(self, mock_urlopen):
        result = _resolve_channel_names("xoxb-test", ["C0AAA", "C0BBB"])
        assert result == ["C0AAA", "C0BBB"]
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_resolve_looks_up_names(self, mock_urlopen):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({
                "ok": True,
                "channels": [
                    {"id": "C_SUPPORT", "name": "support"},
                    {"id": "C_ALERTS", "name": "alerts"},
                ],
                "response_metadata": {"next_cursor": ""},
            }).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        result = _resolve_channel_names("xoxb-test", ["#support", "alerts"])
        assert result == ["C_SUPPORT", "C_ALERTS"]

    @patch("urllib.request.urlopen")
    def test_resolve_mixed_ids_and_names(self, mock_urlopen):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({
                "ok": True,
                "channels": [{"id": "C_SUPPORT", "name": "support"}],
                "response_metadata": {"next_cursor": ""},
            }).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        result = _resolve_channel_names("xoxb-test", ["C0AAA", "#support"])
        assert result == ["C0AAA", "C_SUPPORT"]

    @patch("urllib.request.urlopen")
    def test_resolve_drops_unresolvable_names(self, mock_urlopen):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({
                "ok": True,
                "channels": [],
                "response_metadata": {"next_cursor": ""},
            }).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        result = _resolve_channel_names("xoxb-test", ["#nonexistent"])
        assert result == []


def test_slack_keys_helper():
    from modastack.events.adapters import _slack_keys
    assert _slack_keys("T1", []) == ["slack:T1"]
    assert _slack_keys("T1", ["C1", "C2"]) == ["slack:T1:C1", "slack:T1:C2"]
    assert _slack_keys("", ["C1"]) == []


class TestLinearDetector:

    @patch("urllib.request.urlopen")
    def test_detect_with_key(self, mock_urlopen, tmp_path):
        import json
        mock_resp = type("Resp", (), {
            "read": lambda self: json.dumps({
                "data": {"teams": {"nodes": [{"key": "ENG"}, {"key": "OPS"}]}}
            }).encode(),
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        mock_urlopen.return_value = mock_resp

        cfg = Config(services=[
            ServiceConfig(name="linear", credentials={"api_key": "lin_test"}),
        ])
        keys = detect("linear", tmp_path, cfg)
        assert keys == ["linear:ENG", "linear:OPS"]

    def test_detect_without_key(self, tmp_path):
        cfg = Config(services=[ServiceConfig(name="linear")])
        keys = detect("linear", tmp_path, cfg)
        assert keys == []
