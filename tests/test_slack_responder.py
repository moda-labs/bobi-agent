"""Tests for SlackResponder — routing manager responses to Slack."""

import json
from unittest.mock import patch, MagicMock

from modastack.manager.events.slack_responder import (
    SlackResponder, _markdown_to_slack, _post_to_slack,
)


class TestMarkdownToSlack:

    def test_headings(self):
        assert _markdown_to_slack("# Title") == "*Title*"
        assert _markdown_to_slack("### Sub") == "*Sub*"

    def test_bold(self):
        assert _markdown_to_slack("**bold**") == "*bold*"

    def test_links(self):
        result = _markdown_to_slack("[click](https://example.com)")
        assert result == "<https://example.com|click>"

    def test_truncation(self):
        long_text = "x" * 4000
        result = _markdown_to_slack(long_text)
        assert len(result) <= 3020
        assert "_(truncated)_" in result

    def test_short_text_unchanged(self):
        assert _markdown_to_slack("hello") == "hello"


class TestPostToSlack:

    @patch("modastack.manager.events.slack_responder.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _post_to_slack("xoxb-test", "D123", "hello")
        assert result is True

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["channel"] == "D123"
        assert body["text"] == "hello"
        assert "thread_ts" not in body

    @patch("modastack.manager.events.slack_responder.urllib.request.urlopen")
    def test_with_thread(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _post_to_slack("xoxb-test", "C123", "reply", thread_ts="123.456")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["thread_ts"] == "123.456"

    @patch("modastack.manager.events.slack_responder.urllib.request.urlopen")
    def test_api_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": False, "error": "channel_not_found"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _post_to_slack("xoxb-test", "INVALID", "hello")
        assert result is False


class TestSlackResponder:

    def _make_slack_event(self, text="hi", channel="D123", workspace="T123",
                          etype="slack.dm", thread_ts="", ts="100.001"):
        return {
            "type": etype, "source": "slack",
            "data": {
                "from": "Zach", "text": text, "channel": channel,
                "workspace": workspace, "ts": ts, "thread_ts": thread_ts,
            },
        }

    def _make_github_event(self):
        return {
            "type": "task.opened", "source": "github",
            "data": {"issue_id": "1", "title": "Bug"},
        }

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_replies_to_dm(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        responder = SlackResponder()
        events = [self._make_slack_event()]
        responder.handle(events, "Hello back!")

        mock_post.assert_called_once_with("xoxb-test", "D123", "Hello back!", "")

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_mention_replies_in_thread(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        responder = SlackResponder()
        events = [self._make_slack_event(etype="slack.mention", ts="200.001")]
        responder.handle(events, "On it!")

        mock_post.assert_called_once_with("xoxb-test", "D123", "On it!", "200.001")

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_thread_reply_uses_thread_ts(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        responder = SlackResponder()
        events = [self._make_slack_event(etype="slack.thread_reply", thread_ts="300.001")]
        responder.handle(events, "Got it")

        mock_post.assert_called_once_with("xoxb-test", "D123", "Got it", "300.001")

    @patch("modastack.manager.events.slack_responder._post_to_slack")
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_skips_non_slack_events(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()

        responder = SlackResponder()
        events = [self._make_github_event()]
        responder.handle(events, "Some response")

        mock_post.assert_not_called()

    @patch("modastack.manager.events.slack_responder._post_to_slack")
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_skips_empty_response(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()

        responder = SlackResponder()
        events = [self._make_slack_event()]
        responder.handle(events, "")

        mock_post.assert_not_called()

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_mixed_batch_only_replies_to_slack(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        responder = SlackResponder()
        events = [
            self._make_github_event(),
            self._make_slack_event(channel="D456"),
            self._make_github_event(),
        ]
        responder.handle(events, "Response")

        mock_post.assert_called_once_with("xoxb-test", "D456", "Response", "")

    @patch("modastack.manager.events.slack_responder._post_to_slack")
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_uses_workspace_token(self, mock_config, mock_post):
        mock_cfg = MagicMock()
        mock_cfg.slack_token_for.side_effect = lambda w: {
            "T_A": "xoxb-a", "T_B": "xoxb-b",
        }.get(w, "")
        mock_config.load.return_value = mock_cfg
        mock_post.return_value = True

        responder = SlackResponder()
        events = [
            self._make_slack_event(workspace="T_A", channel="D1"),
            self._make_slack_event(workspace="T_B", channel="D2"),
        ]
        responder.handle(events, "Reply")

        assert mock_post.call_count == 2
        mock_post.assert_any_call("xoxb-a", "D1", "Reply", "")
        mock_post.assert_any_call("xoxb-b", "D2", "Reply", "")

    @patch("modastack.manager.events.slack_responder._post_to_slack")
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_no_token_skips(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = ""

        responder = SlackResponder()
        events = [self._make_slack_event()]
        responder.handle(events, "Response")

        mock_post.assert_not_called()
