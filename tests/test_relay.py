"""Tests for the chat relay module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from modastack.relay import NullAdapter, SlackAdapter, ChatAdapter, build_adapter


class TestNullAdapter:

    def test_is_chat_adapter(self):
        assert isinstance(NullAdapter(), ChatAdapter)

    def test_send_is_noop(self):
        NullAdapter().send("hello")
        NullAdapter().send("hello", role="user")


class TestSlackAdapter:

    def test_is_chat_adapter(self):
        adapter = SlackAdapter("xoxb-test", "C123")
        assert isinstance(adapter, ChatAdapter)

    @patch("modastack.relay.httpx.Client")
    def test_send_posts_to_slack(self, MockClient):
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            json=lambda: {"ok": True, "ts": "123"}
        )
        MockClient.return_value = mock_client

        adapter = SlackAdapter("xoxb-test", "C123")
        adapter.send("hello world")

        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://slack.com/api/chat.postMessage"
        assert kwargs["json"]["channel"] == "C123"
        assert kwargs["json"]["text"] == "hello world"

    @patch("modastack.relay.httpx.Client")
    def test_skips_empty_text(self, MockClient):
        mock_client = MagicMock()
        MockClient.return_value = mock_client

        adapter = SlackAdapter("xoxb-test", "C123")
        adapter.send("")
        adapter.send("   ")

        mock_client.post.assert_not_called()

    @patch("modastack.relay.httpx.Client")
    def test_truncates_long_text(self, MockClient):
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            json=lambda: {"ok": True}
        )
        MockClient.return_value = mock_client

        adapter = SlackAdapter("xoxb-test", "C123")
        adapter.send("x" * 5000)

        _, kwargs = mock_client.post.call_args
        assert len(kwargs["json"]["text"]) < 3100


class TestBuildAdapter:

    @patch("modastack.config.resolve_slack_identity")
    @patch("modastack.config.LocalConfig")
    @patch("modastack.sdk.get_project_root")
    def test_builds_slack_when_configured(self, mock_root, mock_local, mock_resolve):
        mock_root.return_value = Path("/tmp/repo")
        mock_local.load.return_value = MagicMock(
            slack_bot_token="xoxb-test", operator_email="test@test.com",
        )
        mock_resolve.return_value = MagicMock(user_id="U123", dm_channel="C123")

        adapter = build_adapter()
        assert isinstance(adapter, SlackAdapter)

    @patch("modastack.config.LocalConfig")
    @patch("modastack.sdk.get_project_root")
    def test_returns_null_when_no_token(self, mock_root, mock_local):
        mock_root.return_value = Path("/tmp/repo")
        mock_local.load.return_value = MagicMock(
            slack_bot_token="", operator_email="",
        )

        adapter = build_adapter()
        assert isinstance(adapter, NullAdapter)

    @patch("modastack.sdk.get_project_root")
    def test_returns_null_when_no_repo(self, mock_root):
        mock_root.return_value = None

        adapter = build_adapter()
        assert isinstance(adapter, NullAdapter)
