"""Tests for Slack placeholder + typing status features (#189).

Covers:
- update_slack_message (chat.update)
- set_thread_status (assistant.threads.setStatus)
- post_placeholder (post + setStatus in one call)
- StatusRefreshLoop (periodic status refresh)
- CLI --edit flag on slack-reply
- Drain loop placeholder integration
"""

import json
import time
from unittest.mock import patch, MagicMock, call

import pytest

from modastack.slack import (
    format_slack_message,
    post_slack_message,
    update_slack_message,
    set_thread_status,
    post_placeholder,
    StatusRefreshLoop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_urlopen(response_data):
    """Create a mock urlopen that returns the given response dict."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


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
# update_slack_message
# ---------------------------------------------------------------------------

class TestUpdateSlackMessage:
    @patch("urllib.request.urlopen")
    def test_basic_update(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        result = update_slack_message(
            "xoxb-test", "C123", "1720165787.123456", "Real response"
        )
        assert result["ok"] is True

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://slack.com/api/chat.update"
        body = json.loads(req.data)
        assert body["channel"] == "C123"
        assert body["ts"] == "1720165787.123456"
        assert body["text"] == "Real response"

    @patch("urllib.request.urlopen")
    def test_formats_markdown(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        update_slack_message(
            "xoxb-test", "C123", "171.42", "**bold** and [link](https://example.com)"
        )

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert "*bold*" in body["text"]
        assert "<https://example.com|link>" in body["text"]

    @patch("urllib.request.urlopen")
    def test_api_error_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(
            {"ok": False, "error": "message_not_found"}
        )

        with pytest.raises(RuntimeError, match="message_not_found"):
            update_slack_message("xoxb-test", "C123", "171.42", "text")


# ---------------------------------------------------------------------------
# set_thread_status
# ---------------------------------------------------------------------------

class TestSetThreadStatus:
    @patch("urllib.request.urlopen")
    def test_set_status(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        set_thread_status(
            "xoxb-test", "C123", "1720165787.123456", "is thinking..."
        )

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://slack.com/api/assistant.threads.setStatus"
        body = json.loads(req.data)
        assert body["channel_id"] == "C123"
        assert body["thread_ts"] == "1720165787.123456"
        assert body["status"] == "is thinking..."

    @patch("urllib.request.urlopen")
    def test_clear_status(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        set_thread_status("xoxb-test", "C123", "171.42", "")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["status"] == ""

    @patch("urllib.request.urlopen")
    def test_failure_does_not_raise(self, mock_urlopen):
        """setStatus failures are non-fatal (logged, not raised)."""
        mock_urlopen.return_value = _mock_urlopen(
            {"ok": False, "error": "not_allowed"}
        )

        # Should not raise — failures are debug-logged and swallowed
        set_thread_status("xoxb-test", "C123", "171.42", "is thinking...")

    @patch("urllib.request.urlopen")
    def test_no_thread_ts_is_noop(self, mock_urlopen):
        """Without thread_ts, setStatus silently does nothing."""
        set_thread_status("xoxb-test", "C123", "", "is thinking...")

        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# post_placeholder
# ---------------------------------------------------------------------------

class TestPostPlaceholder:
    @patch("modastack.slack.set_thread_status")
    @patch("modastack.slack.post_slack_message")
    def test_posts_and_sets_status(self, mock_post, mock_status):
        mock_post.return_value = {"ok": True, "ts": "171.99"}

        ts = post_placeholder(
            "xoxb-test", "C123", thread_ts="171.42"
        )

        assert ts == "171.99"
        mock_post.assert_called_once_with(
            "xoxb-test", "C123", "Evaluating\u2026",
            thread_ts="171.42",
        )
        mock_status.assert_called_once_with(
            "xoxb-test", "C123", "171.42", "is thinking\u2026",
        )

    @patch("modastack.slack.set_thread_status")
    @patch("modastack.slack.post_slack_message")
    def test_custom_placeholder_text(self, mock_post, mock_status):
        mock_post.return_value = {"ok": True, "ts": "171.99"}

        post_placeholder(
            "xoxb-test", "C123", thread_ts="171.42",
            placeholder_text="Processing...",
        )

        assert mock_post.call_args[0][2] == "Processing..."

    @patch("modastack.slack.set_thread_status")
    @patch("modastack.slack.post_slack_message")
    def test_no_thread_ts_posts_without_status(self, mock_post, mock_status):
        """Without thread context, posts placeholder but skips status."""
        mock_post.return_value = {"ok": True, "ts": "171.99"}

        ts = post_placeholder("xoxb-test", "C123", thread_ts="")

        assert ts == "171.99"
        mock_post.assert_called_once()
        mock_status.assert_not_called()

    @patch("modastack.slack.set_thread_status")
    @patch("modastack.slack.post_slack_message")
    def test_post_failure_returns_empty(self, mock_post, mock_status):
        """If posting fails, return empty string and don't set status."""
        mock_post.side_effect = RuntimeError("network")

        ts = post_placeholder("xoxb-test", "C123", thread_ts="171.42")

        assert ts == ""
        mock_status.assert_not_called()


# ---------------------------------------------------------------------------
# StatusRefreshLoop
# ---------------------------------------------------------------------------

class TestStatusRefreshLoop:
    @patch("modastack.slack.set_thread_status")
    def test_starts_and_stops(self, mock_status):
        loop = StatusRefreshLoop(
            "xoxb-test", "C123", "171.42", interval=0.05
        )
        loop.start()
        assert loop.is_alive()

        loop.stop()
        loop.join(timeout=1)
        assert not loop.is_alive()

    @patch("modastack.slack.set_thread_status")
    def test_refreshes_status_periodically(self, mock_status):
        loop = StatusRefreshLoop(
            "xoxb-test", "C123", "171.42", interval=0.05
        )
        loop.start()
        time.sleep(0.2)
        loop.stop()
        loop.join(timeout=1)

        # Should have been called at least twice in 0.2s with 0.05s interval
        assert mock_status.call_count >= 2
        for c in mock_status.call_args_list:
            assert c == call("xoxb-test", "C123", "171.42", "is thinking\u2026")

    @patch("modastack.slack.set_thread_status")
    def test_stop_clears_status(self, mock_status):
        loop = StatusRefreshLoop(
            "xoxb-test", "C123", "171.42", interval=0.05
        )
        loop.start()
        time.sleep(0.05)
        loop.stop(clear=True)
        loop.join(timeout=1)

        # Last call should clear the status
        last_call = mock_status.call_args_list[-1]
        assert last_call == call("xoxb-test", "C123", "171.42", "")


# ---------------------------------------------------------------------------
# CLI --edit flag
# ---------------------------------------------------------------------------

class TestSlackReplyEdit:
    @patch("urllib.request.urlopen")
    def test_edit_calls_chat_update(self, mock_urlopen, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "C456",
            "-t", "171.42",
            "--edit", "171.99",
            "Real response here",
        ])
        assert result.exit_code == 0, result.output

        # First call should be chat.update (second is setStatus to clear)
        update_req = mock_urlopen.call_args_list[0][0][0]
        assert "chat.update" in update_req.full_url
        body = json.loads(update_req.data)
        assert body["ts"] == "171.99"
        assert body["channel"] == "C456"
        assert body["text"] == "Real response here"

    @patch("modastack.slack.set_thread_status")
    @patch("modastack.slack.update_slack_message")
    def test_edit_clears_thread_status(self, mock_update, mock_status,
                                        tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        mock_update.return_value = {"ok": True}

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "C456",
            "-t", "171.42",
            "--edit", "171.99",
            "Response",
        ])
        assert result.exit_code == 0, result.output

        mock_update.assert_called_once()
        # Should clear thread status
        mock_status.assert_called_once_with(
            "xoxb-test", "C456", "171.42", "",
        )

    @patch("urllib.request.urlopen")
    def test_edit_without_thread_skips_status(self, mock_urlopen,
                                               tmp_path, monkeypatch):
        """--edit without -t still updates the message but skips status clear."""
        _setup_project(tmp_path, monkeypatch)
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "C456",
            "--edit", "171.99",
            "Response",
        ])
        assert result.exit_code == 0, result.output

        req = mock_urlopen.call_args[0][0]
        assert "chat.update" in req.full_url

    @patch("urllib.request.urlopen")
    def test_normal_reply_unchanged(self, mock_urlopen, tmp_path, monkeypatch):
        """Without --edit, slack-reply still posts a new message."""
        _setup_project(tmp_path, monkeypatch)
        mock_urlopen.return_value = _mock_urlopen({"ok": True})

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        result = runner.invoke(main, [
            "slack-reply", "-w", "T123", "-c", "C456",
            "-t", "171.42",
            "Normal reply",
        ])
        assert result.exit_code == 0, result.output

        req = mock_urlopen.call_args[0][0]
        assert "chat.postMessage" in req.full_url


# ---------------------------------------------------------------------------
# Drain loop placeholder integration
# ---------------------------------------------------------------------------

class TestDrainPlaceholder:
    def _make_slack_event(self, channel="C123", thread_ts="171.42",
                          ts="171.50", text="hello bot"):
        return {
            "source": "slack",
            "type": "slack.mention",
            "delivery": "chat",
            "text": text,
            "fields": {
                "user_id": "U123",
                "channel": channel,
                "channel_type": "channel",
                "ts": ts,
                "thread_ts": thread_ts,
            },
        }

    @patch("modastack.slack.StatusRefreshLoop")
    @patch("modastack.slack.post_placeholder")
    def test_placeholder_posted_on_slack_event(self, mock_placeholder, mock_loop_cls,
                                                tmp_path, monkeypatch):
        """Drain loop posts placeholder for Slack chat events."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        mock_placeholder.return_value = "171.99"
        mock_loop = MagicMock()
        mock_loop_cls.return_value = mock_loop

        q = SimpleQueue()
        q.put(self._make_slack_event())

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        def fake_get_token():
            return "xoxb-test"

        monkeypatch.setattr("modastack.events.drain._get_slack_token", fake_get_token)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_placeholder.assert_called_once_with(
            "xoxb-test", "C123", thread_ts="171.42",
        )
        mock_loop.start.assert_called_once()

        # placeholder_ts should appear in the delivered text
        assert "171.99" in delivered[0]

    @patch("modastack.slack.post_placeholder")
    def test_no_placeholder_for_non_slack(self, mock_placeholder,
                                           tmp_path, monkeypatch):
        """Non-Slack events don't get a placeholder."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        q = SimpleQueue()
        q.put({
            "source": "github",
            "type": "github.push",
            "delivery": "chat",
            "text": "push event",
            "fields": {},
        })

        def fake_deliver(session, text, sender=""):
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_placeholder.assert_not_called()

    @patch("modastack.slack.post_placeholder")
    def test_placeholder_failure_still_delivers(self, mock_placeholder,
                                                 tmp_path, monkeypatch):
        """If placeholder posting fails, event is still delivered normally."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        mock_placeholder.side_effect = RuntimeError("network")

        q = SimpleQueue()
        q.put(self._make_slack_event())

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        def fake_get_token():
            return "xoxb-test"

        monkeypatch.setattr("modastack.events.drain._get_slack_token", fake_get_token)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        # Event still delivered despite placeholder failure
        assert len(delivered) == 1
        assert "placeholder_ts" not in delivered[0]

    @patch("modastack.slack.post_placeholder")
    def test_no_placeholder_without_token(self, mock_placeholder,
                                           tmp_path, monkeypatch):
        """No placeholder if Slack token is unavailable."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        q = SimpleQueue()
        q.put(self._make_slack_event())

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        def fake_get_token():
            return ""

        monkeypatch.setattr("modastack.events.drain._get_slack_token", fake_get_token)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_placeholder.assert_not_called()
        assert len(delivered) == 1

    @patch("modastack.slack.StatusRefreshLoop")
    @patch("modastack.slack.post_placeholder")
    def test_placeholder_uses_ts_when_no_thread_ts(self, mock_placeholder,
                                                     mock_loop_cls,
                                                     tmp_path, monkeypatch):
        """When thread_ts is empty, uses ts as the thread anchor."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        mock_placeholder.return_value = "171.99"
        mock_loop_cls.return_value = MagicMock()

        q = SimpleQueue()
        q.put(self._make_slack_event(thread_ts="", ts="171.50"))

        def fake_deliver(session, text, sender=""):
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        def fake_get_token():
            return "xoxb-test"

        monkeypatch.setattr("modastack.events.drain._get_slack_token", fake_get_token)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_placeholder.assert_called_once_with(
            "xoxb-test", "C123", thread_ts="171.50",
        )
