"""Tests for Slack placeholder + typing status features (#189).

Covers:
- update_slack_message (chat.update)
- set_thread_status (assistant.threads.setStatus)
- post_placeholder (post + setStatus in one call)
- StatusRefreshLoop (periodic status refresh)
- SlackInputChannel (framework-level channel handler)
- CLI --edit flag on slack-reply
- Drain loop integration via channel handlers
"""

import json
import time
from unittest.mock import patch, MagicMock, call

import httpx
import pytest

from modastack import http as pooled
from modastack.slack import (
    post_slack_message,
    update_slack_message,
    set_thread_status,
    post_placeholder,
    StatusRefreshLoop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(response_data, *, requests_log=None, side_effect=None):
    """Create an httpx.Client with a MockTransport returning *response_data*.

    If *requests_log* is provided (a list), each incoming request is
    appended so callers can inspect URL, headers and body.

    If *side_effect* is provided it should be a callable(request) that is
    invoked instead of returning the canned response (useful for raising).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if requests_log is not None:
            requests_log.append(request)
        if side_effect is not None:
            return side_effect(request)
        return httpx.Response(200, json=response_data)

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


def _make_slack_event(channel="C123", thread_ts="171.42",
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


# ---------------------------------------------------------------------------
# update_slack_message
# ---------------------------------------------------------------------------

class TestUpdateSlackMessage:
    def test_basic_update(self):
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        with patch.object(pooled, '_client', mock_client):
            result = update_slack_message(
                "xoxb-test", "C123", "1720165787.123456", "Real response"
            )
        assert result["ok"] is True

        req = reqs[0]
        assert str(req.url) == "https://slack.com/api/chat.update"
        body = json.loads(req.content)
        assert body["channel"] == "C123"
        assert body["ts"] == "1720165787.123456"
        assert body["text"] == "Real response"

    def test_formats_markdown(self):
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        with patch.object(pooled, '_client', mock_client):
            update_slack_message(
                "xoxb-test", "C123", "171.42", "**bold** and [link](https://example.com)"
            )

        body = json.loads(reqs[0].content)
        assert "*bold*" in body["text"]
        assert "<https://example.com|link>" in body["text"]

    def test_api_error_raises(self):
        mock_client = _make_mock_client({"ok": False, "error": "message_not_found"})

        with patch.object(pooled, '_client', mock_client):
            with pytest.raises(RuntimeError, match="message_not_found"):
                update_slack_message("xoxb-test", "C123", "171.42", "text")


# ---------------------------------------------------------------------------
# set_thread_status
# ---------------------------------------------------------------------------

class TestSetThreadStatus:
    def test_set_status(self):
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        with patch.object(pooled, '_client', mock_client):
            set_thread_status(
                "xoxb-test", "C123", "1720165787.123456", "is thinking..."
            )

        req = reqs[0]
        assert str(req.url) == "https://slack.com/api/assistant.threads.setStatus"
        body = json.loads(req.content)
        assert body["channel_id"] == "C123"
        assert body["thread_ts"] == "1720165787.123456"
        assert body["status"] == "is thinking..."

    def test_clear_status(self):
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        with patch.object(pooled, '_client', mock_client):
            set_thread_status("xoxb-test", "C123", "171.42", "")

        body = json.loads(reqs[0].content)
        assert body["status"] == ""

    def test_failure_does_not_raise(self):
        """setStatus failures are non-fatal (logged, not raised)."""
        mock_client = _make_mock_client({"ok": False, "error": "not_allowed"})

        with patch.object(pooled, '_client', mock_client):
            # Should not raise — failures are debug-logged and swallowed
            set_thread_status("xoxb-test", "C123", "171.42", "is thinking...")

    def test_no_thread_ts_is_noop(self):
        """Without thread_ts, setStatus silently does nothing."""
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        with patch.object(pooled, '_client', mock_client):
            set_thread_status("xoxb-test", "C123", "", "is thinking...")

        assert len(reqs) == 0


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

    @patch("modastack.slack.set_thread_status")
    def test_self_terminates_at_max_seconds(self, mock_status):
        # Safety cap: even if nothing ever calls stop(), the loop must clear
        # the status and exit on its own — no forever "is thinking…".
        loop = StatusRefreshLoop(
            "xoxb-test", "C123", "171.42", interval=0.02, max_seconds=0.05,
        )
        loop.start()
        loop.join(timeout=2)
        assert not loop.is_alive()
        assert mock_status.call_args_list[-1] == call("xoxb-test", "C123", "171.42", "")


# ---------------------------------------------------------------------------
# SlackInputChannel (framework-level channel handler)
# ---------------------------------------------------------------------------

class TestSlackInputChannel:
    @patch("modastack.slack.StatusRefreshLoop")
    @patch("modastack.slack.post_placeholder")
    def test_prepare_posts_placeholder_and_injects_ts(self, mock_placeholder,
                                                       mock_loop_cls):
        """Channel handler posts placeholder and injects placeholder_ts into fields."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.return_value = "171.99"
        mock_loop = MagicMock()
        mock_loop_cls.return_value = mock_loop

        handler = SlackInputChannel()
        event = _make_slack_event()
        result = handler.prepare(event, "xoxb-test")

        mock_placeholder.assert_called_once_with(
            "xoxb-test", "C123", thread_ts="171.42",
        )
        mock_loop_cls.assert_called_once_with("xoxb-test", "C123", "171.42")
        mock_loop.start.assert_called_once()

        # placeholder_ts injected into fields
        assert result["fields"]["placeholder_ts"] == "171.99"
        # Original fields preserved
        assert result["fields"]["channel"] == "C123"
        assert result["fields"]["user_id"] == "U123"

    @patch("modastack.slack.StatusRefreshLoop")
    @patch("modastack.slack.post_placeholder")
    def test_prepare_uses_ts_when_no_thread_ts(self, mock_placeholder,
                                                mock_loop_cls):
        """When thread_ts is empty, uses ts as the thread anchor."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.return_value = "171.99"
        mock_loop_cls.return_value = MagicMock()

        handler = SlackInputChannel()
        event = _make_slack_event(thread_ts="", ts="171.50")
        handler.prepare(event, "xoxb-test")

        mock_placeholder.assert_called_once_with(
            "xoxb-test", "C123", thread_ts="171.50",
        )

    @patch("modastack.slack.post_placeholder")
    def test_prepare_no_refresh_without_thread(self, mock_placeholder):
        """No refresh loop started when there's no thread context."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.return_value = "171.99"

        handler = SlackInputChannel()
        event = _make_slack_event(thread_ts="", ts="171.50")
        result = handler.prepare(event, "xoxb-test")

        assert result["fields"]["placeholder_ts"] == "171.99"

    @patch("modastack.slack.post_placeholder")
    def test_prepare_failure_returns_original_event(self, mock_placeholder):
        """If placeholder posting fails, returns the original event unchanged."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.side_effect = RuntimeError("network")

        handler = SlackInputChannel()
        event = _make_slack_event()
        result = handler.prepare(event, "xoxb-test")

        assert "placeholder_ts" not in result["fields"]

    @patch("modastack.slack.post_placeholder")
    def test_prepare_empty_placeholder_returns_original(self, mock_placeholder):
        """If placeholder returns empty ts, original event is returned."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.return_value = ""

        handler = SlackInputChannel()
        event = _make_slack_event()
        result = handler.prepare(event, "xoxb-test")

        assert "placeholder_ts" not in result["fields"]

    def test_prepare_no_channel_returns_original(self):
        """Events without a channel field are returned unchanged."""
        from modastack.events.channels import SlackInputChannel

        handler = SlackInputChannel()
        event = {"source": "slack", "type": "slack.mention",
                 "delivery": "chat", "fields": {}}
        result = handler.prepare(event, "xoxb-test")

        assert result is event

    @patch("modastack.slack.post_placeholder")
    def test_prepare_does_not_mutate_original(self, mock_placeholder):
        """Channel handler returns a new event dict, doesn't mutate the original."""
        from modastack.events.channels import SlackInputChannel

        mock_placeholder.return_value = "171.99"

        handler = SlackInputChannel()
        event = _make_slack_event()
        original_fields = dict(event["fields"])

        handler.prepare(event, "xoxb-test")

        # Original event's fields should be untouched
        assert "placeholder_ts" not in event["fields"]
        assert event["fields"] == original_fields


# ---------------------------------------------------------------------------
# Channel handler registry
# ---------------------------------------------------------------------------

class TestChannelRegistry:
    def test_slack_handler_registered(self):
        from modastack.events.channels import get_channel_handler
        handler = get_channel_handler("slack")
        assert handler is not None

    def test_slack_handler_credential_key(self):
        from modastack.events.channels import get_channel_handler
        handler = get_channel_handler("slack")
        assert handler.credential_key == "bot_token"

    def test_unknown_source_returns_none(self):
        from modastack.events.channels import get_channel_handler
        assert get_channel_handler("github") is None
        assert get_channel_handler("unknown") is None


class TestStopRefreshLoop:
    def test_stops_and_removes_active_loop(self):
        from modastack.events.channels import _active_loops, stop_refresh_loop

        mock_loop = MagicMock()
        _active_loops[("C123", "171.42")] = mock_loop

        stop_refresh_loop("C123", "171.42")

        mock_loop.stop.assert_called_once_with(clear=True)
        assert ("C123", "171.42") not in _active_loops

    def test_noop_when_no_loop_exists(self):
        from modastack.events.channels import stop_refresh_loop

        # Should not raise
        stop_refresh_loop("C999", "999.99")

    def test_stop_all_stops_every_loop(self):
        # This is what actually clears the indicator on turn completion (the
        # per-reply stop_refresh_loop runs in the CLI subprocess and can't
        # reach the manager's loops).
        from modastack.events.channels import _active_loops, stop_all_refresh_loops

        l1, l2 = MagicMock(), MagicMock()
        _active_loops[("C1", "1.1")] = l1
        _active_loops[("C2", "2.2")] = l2

        stop_all_refresh_loops()

        l1.stop.assert_called_once_with(clear=True)
        l2.stop.assert_called_once_with(clear=True)
        assert _active_loops == {}


# ---------------------------------------------------------------------------
# CLI --edit flag
# ---------------------------------------------------------------------------

class TestSlackReplyEdit:
    def test_edit_calls_chat_update(self, tmp_path, monkeypatch):
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        with patch.object(pooled, '_client', mock_client):
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "C456",
                "-t", "171.42",
                "--edit", "171.99",
                "Real response here",
            ])
        assert result.exit_code == 0, result.output

        # First call should be chat.update (second is setStatus to clear)
        update_req = reqs[0]
        assert "chat.update" in str(update_req.url)
        body = json.loads(update_req.content)
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

    def test_edit_without_thread_skips_status(self, tmp_path, monkeypatch):
        """--edit without -t still updates the message but skips status clear."""
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        with patch.object(pooled, '_client', mock_client):
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "C456",
                "--edit", "171.99",
                "Response",
            ])
        assert result.exit_code == 0, result.output

        assert any("chat.update" in str(r.url) for r in reqs)

    def test_normal_reply_unchanged(self, tmp_path, monkeypatch):
        """Without --edit, slack-reply still posts a new message."""
        _setup_project(tmp_path, monkeypatch)
        reqs = []
        mock_client = _make_mock_client({"ok": True}, requests_log=reqs)

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        with patch.object(pooled, '_client', mock_client):
            result = runner.invoke(main, [
                "slack-reply", "-w", "T123", "-c", "C456",
                "-t", "171.42",
                "Normal reply",
            ])
        assert result.exit_code == 0, result.output

        assert any("chat.postMessage" in str(r.url) for r in reqs)


# ---------------------------------------------------------------------------
# Drain loop integration (channel handler wiring)
# ---------------------------------------------------------------------------

class _FakeConfig:
    """Minimal Config stand-in for drain loop tests."""

    def __init__(self, credentials=None):
        self._creds = credentials or {}

    def credential(self, service, key):
        return self._creds.get((service, key), "")


class TestDrainChannelIntegration:
    @patch("modastack.events.channels.SlackInputChannel.prepare")
    def test_drain_calls_channel_handler_for_slack(self, mock_prepare,
                                                    monkeypatch):
        """Drain loop invokes the Slack channel handler for chat events."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        event = _make_slack_event()
        augmented = dict(event, fields=dict(event["fields"], placeholder_ts="171.99"))
        mock_prepare.return_value = augmented

        q = SimpleQueue()
        q.put(event)

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        cfg = _FakeConfig({("slack", "bot_token"): "xoxb-test"})
        monkeypatch.setattr("modastack.events.drain._get_project_config", lambda: cfg)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_prepare.assert_called_once_with(event, "xoxb-test")
        # placeholder_ts should appear in the delivered text via formatter
        assert "placeholder_ts" in delivered[0]
        assert "171.99" in delivered[0]

    def test_drain_skips_handler_for_non_slack(self, monkeypatch):
        """Non-Slack events are delivered without channel handler processing."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        q = SimpleQueue()
        q.put({
            "source": "github",
            "type": "github.push",
            "delivery": "chat",
            "text": "push event",
            "fields": {"repo": "org/repo"},
        })

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        assert len(delivered) == 1
        assert "github.push" in delivered[0]

    @patch("modastack.events.channels.SlackInputChannel.prepare")
    def test_drain_skips_handler_without_token(self, mock_prepare,
                                                monkeypatch):
        """No channel handler called when service token is unavailable."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        q = SimpleQueue()
        q.put(_make_slack_event())

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        cfg = _FakeConfig()  # No credentials configured
        monkeypatch.setattr("modastack.events.drain._get_project_config", lambda: cfg)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_prepare.assert_not_called()
        assert len(delivered) == 1

    @patch("modastack.events.channels.SlackInputChannel.prepare")
    def test_drain_deduplicates_placeholders_per_thread(self, mock_prepare,
                                                         monkeypatch):
        """When multiple Slack events for the same thread are batched,
        only one placeholder is posted and all events share its ts."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop, DRAIN_INTERVAL

        e1 = _make_slack_event(ts="171.50", text="first message")
        e2 = _make_slack_event(ts="171.51", text="second message")

        # prepare() is called only for the first event; returns augmented copy
        augmented = dict(e1, fields=dict(e1["fields"], placeholder_ts="171.99"))
        mock_prepare.return_value = augmented

        q = SimpleQueue()
        q.put(e1)
        q.put(e2)

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        cfg = _FakeConfig({("slack", "bot_token"): "xoxb-test"})
        monkeypatch.setattr("modastack.events.drain._get_project_config", lambda: cfg)
        monkeypatch.setattr("modastack.events.drain.DRAIN_INTERVAL", 0)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        # prepare() should only be called once (for the first event)
        mock_prepare.assert_called_once_with(e1, "xoxb-test")
        # Both events should appear in the delivered text with placeholder_ts
        assert len(delivered) == 1  # all chat events delivered in one batch
        assert delivered[0].count("placeholder_ts") == 2
        assert delivered[0].count("171.99") == 2

    @patch("modastack.events.channels.SlackInputChannel.prepare")
    def test_drain_separate_placeholders_for_different_threads(self, mock_prepare,
                                                                monkeypatch):
        """Events in different threads each get their own placeholder."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        e1 = _make_slack_event(thread_ts="171.42", ts="171.50")
        e2 = _make_slack_event(thread_ts="172.00", ts="172.01")

        aug1 = dict(e1, fields=dict(e1["fields"], placeholder_ts="p1"))
        aug2 = dict(e2, fields=dict(e2["fields"], placeholder_ts="p2"))
        mock_prepare.side_effect = [aug1, aug2]

        q = SimpleQueue()
        q.put(e1)
        q.put(e2)

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        cfg = _FakeConfig({("slack", "bot_token"): "xoxb-test"})
        monkeypatch.setattr("modastack.events.drain._get_project_config", lambda: cfg)
        monkeypatch.setattr("modastack.events.drain.DRAIN_INTERVAL", 0)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        # prepare() should be called once per distinct thread
        assert mock_prepare.call_count == 2

    @patch("modastack.events.channels.SlackInputChannel.prepare")
    def test_drain_skips_handler_without_config(self, mock_prepare,
                                                 monkeypatch):
        """No channel handler called when project config is unavailable."""
        from queue import SimpleQueue
        from modastack.events.drain import drain_loop

        q = SimpleQueue()
        q.put(_make_slack_event())

        delivered = []

        def fake_deliver(session, text, sender=""):
            delivered.append(text)
            raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        monkeypatch.setattr("modastack.events.drain._get_project_config", lambda: None)

        with patch("modastack.inbox.deliver", fake_deliver):
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)

        mock_prepare.assert_not_called()
        assert len(delivered) == 1
