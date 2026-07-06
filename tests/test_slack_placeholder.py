"""Tests for the chat typing flow.

Covers:
- SlackInputChannel (gateway-backed typing response context)
- TypingRefreshLoop (periodic /channels/typing refresh)
- stop_all_refresh_loops (turn-end sweep)
- Drain loop integration via channel handlers

The handlers are policy shims over the channel gateway: they address events
by their ``conversation`` reference and never touch a Slack token.
"""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from bobi.events.channels import (
    SlackInputChannel,
    TypingRefreshLoop,
    get_channel_handler,
    stop_all_refresh_loops,
    _active_loops,
)
from bobi.events.gateway import GatewayError


PROJECT = Path("/tmp/fake-project")
CONV = "slack:T123:channel:C123:thread:171.42"


@pytest.fixture(autouse=True)
def _clean_loops():
    yield
    stop_all_refresh_loops()


def _make_slack_event(channel="C123", thread_ts="171.42",
                      ts="171.50", text="hello bot", conversation=CONV):
    event = {
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
    if conversation:
        event["conversation"] = conversation
    return event


# ---------------------------------------------------------------------------
# TypingRefreshLoop
# ---------------------------------------------------------------------------

class TestTypingRefreshLoop:
    @patch("bobi.events.gateway.channels_typing")
    def test_starts_and_stops(self, mock_typing):
        loop = TypingRefreshLoop(PROJECT, CONV, interval=0.05)
        loop.start()
        assert loop.is_alive()
        loop.stop()
        loop.join(timeout=2)
        assert not loop.is_alive()

    @patch("bobi.events.gateway.channels_typing")
    def test_refreshes_typing_periodically(self, mock_typing):
        loop = TypingRefreshLoop(PROJECT, CONV, interval=0.05)
        loop.start()
        time.sleep(0.18)
        loop.stop()
        loop.join(timeout=2)
        assert mock_typing.call_count >= 2
        mock_typing.assert_has_calls([call(PROJECT, CONV, True)])

    @patch("bobi.events.gateway.channels_typing")
    def test_stop_clears_typing(self, mock_typing):
        loop = TypingRefreshLoop(PROJECT, CONV, interval=5)
        loop.start()
        loop.stop(clear=True)
        loop.join(timeout=2)
        mock_typing.assert_called_with(PROJECT, CONV, False)

    @patch("bobi.events.gateway.channels_typing")
    def test_self_terminates_at_max_seconds(self, mock_typing):
        loop = TypingRefreshLoop(PROJECT, CONV, interval=0.02, max_seconds=0.01)
        loop.start()
        loop.join(timeout=2)
        assert not loop.is_alive()
        # The safety cap clears the indicator on the way out.
        mock_typing.assert_called_with(PROJECT, CONV, False)


# ---------------------------------------------------------------------------
# SlackInputChannel
# ---------------------------------------------------------------------------

class TestSlackInputChannel:
    @patch("bobi.events.gateway.channels_typing", return_value=True)
    @patch("bobi.events.gateway.channels_send")
    def test_prepare_starts_typing_without_placeholder(self, mock_send, mock_typing):
        handler = SlackInputChannel()
        event = _make_slack_event()

        prepared = handler.prepare(event, PROJECT)

        mock_send.assert_not_called()
        assert "placeholder_ts" not in prepared["fields"]
        # Typing indicator set + refresh loop registered under the ref.
        mock_typing.assert_called_with(PROJECT, CONV, True)
        assert CONV in _active_loops

    @patch("bobi.events.gateway.channels_typing", return_value=True)
    @patch("bobi.events.gateway.channels_send")
    def test_prepare_reuses_existing_refresh_loop(self, mock_send, mock_typing):
        handler = SlackInputChannel()

        handler.prepare(_make_slack_event(), PROJECT)
        first_loop = _active_loops[CONV]
        handler.prepare(_make_slack_event(ts="171.51"), PROJECT)
        mock_send.assert_not_called()
        assert _active_loops[CONV] is first_loop

    @patch("bobi.events.gateway.channels_typing", return_value=True)
    @patch("bobi.events.gateway.channels_send")
    def test_prepare_replaces_dead_refresh_loop(self, mock_send, mock_typing):
        """A loop that self-terminated at its safety cap must not block a
        fresh one for a later event in the same conversation."""
        handler = SlackInputChannel()

        dead = TypingRefreshLoop(PROJECT, CONV, interval=0.01, max_seconds=0.01)
        dead.start()
        dead.join(timeout=2)
        assert not dead.is_alive()
        _active_loops[CONV] = dead

        handler.prepare(_make_slack_event(), PROJECT)
        mock_send.assert_not_called()
        assert _active_loops[CONV] is not dead
        assert _active_loops[CONV].is_alive()

    @patch("bobi.events.gateway.channels_typing")
    def test_prepare_failure_returns_event_without_placeholder(self, mock_typing):
        mock_typing.side_effect = GatewayError("event server unreachable")
        handler = SlackInputChannel()
        event = _make_slack_event()
        event["fields"]["placeholder_ts"] = "stale"

        prepared = handler.prepare(event, PROJECT)

        assert prepared is not event
        assert "placeholder_ts" not in prepared["fields"]
        assert event["fields"]["placeholder_ts"] == "stale"

    @patch("bobi.events.gateway.channels_send")
    def test_prepare_strips_stale_placeholder_from_mention(self, mock_send):
        handler = SlackInputChannel()
        event = _make_slack_event()
        event["fields"]["placeholder_ts"] = "stale"

        prepared = handler.prepare(event, PROJECT)
        mock_send.assert_not_called()
        assert "placeholder_ts" not in prepared["fields"]
        assert event["fields"]["placeholder_ts"] == "stale"

    def test_prepare_no_conversation_returns_original(self):
        handler = SlackInputChannel()
        event = _make_slack_event(conversation="")

        prepared = handler.prepare(event, PROJECT)
        assert prepared is event

    @patch("bobi.events.gateway.channels_send")
    def test_prepare_skips_passive_thread_reply(self, mock_send):
        handler = SlackInputChannel()
        event = _make_slack_event()
        event["type"] = "slack.thread_reply"

        prepared = handler.prepare(event, PROJECT)

        mock_send.assert_not_called()
        assert "placeholder_ts" not in prepared["fields"]

    @patch("bobi.events.gateway.channels_send")
    def test_prepare_strips_stale_placeholder_from_thread_reply(self, mock_send):
        handler = SlackInputChannel()
        event = _make_slack_event()
        event["type"] = "slack.thread_reply"
        event["fields"]["placeholder_ts"] = "stale"

        prepared = handler.prepare(event, PROJECT)

        assert prepared is not event
        assert "placeholder_ts" not in prepared["fields"]
        assert event["fields"]["placeholder_ts"] == "stale"

    @patch("bobi.events.gateway.channels_typing", return_value=True)
    @patch("bobi.events.gateway.channels_send")
    def test_prepare_does_not_mutate_original(self, mock_send, mock_typing):
        handler = SlackInputChannel()
        event = _make_slack_event()
        event["fields"]["placeholder_ts"] = "stale"

        prepared = handler.prepare(event, PROJECT)

        assert prepared is not event
        assert event["fields"]["placeholder_ts"] == "stale"
        assert "placeholder_ts" not in prepared["fields"]


# ---------------------------------------------------------------------------
# Registry + turn-end sweep
# ---------------------------------------------------------------------------

class TestChannelRegistry:
    def test_slack_handler_registered(self):
        handler = get_channel_handler("slack")
        assert isinstance(handler, SlackInputChannel)

    def test_unknown_source_returns_none(self):
        assert get_channel_handler("github") is None


class TestStopAllRefreshLoops:
    @patch("bobi.events.gateway.channels_typing", return_value=True)
    def test_stop_all_stops_every_loop(self, mock_typing):
        loops = []
        for conv in ("slack:T1:dm:D1:thread:1.1", "slack:T1:dm:D2:thread:2.2"):
            loop = TypingRefreshLoop(PROJECT, conv, interval=5)
            loop.start()
            _active_loops[conv] = loop
            loops.append(loop)

        stop_all_refresh_loops()

        for loop in loops:
            loop.join(timeout=2)
            assert not loop.is_alive()
        assert not _active_loops


# ---------------------------------------------------------------------------
# Drain loop integration (channel handler wiring)
# ---------------------------------------------------------------------------

class TestDrainChannelIntegration:
    @patch("bobi.events.channels.SlackInputChannel.prepare")
    def test_drain_calls_channel_handler_for_slack(self, mock_prepare,
                                                    monkeypatch):
        """Drain loop invokes the Slack channel handler for chat events."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        event = _make_slack_event()
        mock_prepare.return_value = event

        q = SimpleQueue()
        q.put(event)

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        mock_prepare.assert_called_once_with(event, PROJECT)
        assert "placeholder_ts" not in delivered[0]

    def test_drain_skips_handler_for_non_slack(self, monkeypatch):
        """Non-Slack events are delivered without channel handler processing."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        q = SimpleQueue()
        q.put({
            "source": "github",
            "type": "github.push",
            "delivery": "chat",
            "text": "push event",
            "fields": {"repo": "org/repo"},
        })

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        assert len(delivered) == 1
        assert "github.push" in delivered[0]

    @patch("bobi.events.channels.SlackInputChannel.prepare")
    def test_drain_prepares_each_active_slack_event(self, mock_prepare,
                                                    monkeypatch):
        """When multiple Slack events for the same thread are batched,
        each active event gets typing-only preparation."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        e1 = _make_slack_event(ts="171.50", text="first message")
        e2 = _make_slack_event(ts="171.51", text="second message")

        mock_prepare.side_effect = [e1, e2]

        q = SimpleQueue()
        q.put(e1)
        q.put(e2)

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)
        monkeypatch.setattr("bobi.events.drain.DRAIN_INTERVAL", 0)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        assert mock_prepare.call_count == 2
        mock_prepare.assert_any_call(e1, PROJECT)
        mock_prepare.assert_any_call(e2, PROJECT)
        # Both events should appear in the delivered text without placeholder_ts.
        assert len(delivered) == 1  # all chat events delivered in one batch
        assert "placeholder_ts" not in delivered[0]

    @patch("bobi.events.gateway.channels_typing", return_value=True)
    @patch("bobi.events.gateway.channels_send")
    def test_prepare_chat_events_keeps_thread_reply_passive(self,
                                                             mock_send,
                                                             mock_typing,
                                                             monkeypatch):
        """Mixed batches keep placeholder metadata off passive thread replies."""
        from bobi.events.drain import _prepare_chat_events

        active = _make_slack_event(ts="171.50", text="active mention")
        passive = _make_slack_event(ts="171.51", text="passive chatter")
        passive["type"] = "slack.thread_reply"

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)

        prepared = _prepare_chat_events([active, passive])

        assert prepared[0]["type"] == "slack.mention"
        assert "placeholder_ts" not in prepared[0]["fields"]
        assert prepared[1] is not passive
        assert prepared[1]["type"] == "slack.thread_reply"
        assert "placeholder_ts" not in prepared[1]["fields"]
        mock_send.assert_not_called()

    def test_prepare_chat_events_strips_stale_thread_reply_placeholder(self,
                                                                       monkeypatch):
        """Passive replies do not deliver stale placeholder metadata."""
        from bobi.events.drain import _prepare_chat_events

        passive = _make_slack_event(ts="171.51", text="passive chatter")
        passive["type"] = "slack.thread_reply"
        passive["fields"]["placeholder_ts"] = "stale"

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)

        prepared = _prepare_chat_events([passive])

        assert prepared[0] is not passive
        assert prepared[0]["type"] == "slack.thread_reply"
        assert "placeholder_ts" not in prepared[0]["fields"]
        assert passive["fields"]["placeholder_ts"] == "stale"

    @patch("bobi.events.channels.SlackInputChannel.prepare")
    def test_drain_does_not_reuse_placeholder_for_thread_reply(self,
                                                                mock_prepare,
                                                                monkeypatch):
        """Passive thread replies must not inherit another event's placeholder."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        e1 = _make_slack_event(ts="171.50", text="active mention")
        e2 = _make_slack_event(ts="171.51", text="passive chatter")
        e2["type"] = "slack.thread_reply"

        mock_prepare.return_value = e1

        q = SimpleQueue()
        q.put(e1)
        q.put(e2)

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)
        monkeypatch.setattr("bobi.events.drain.DRAIN_INTERVAL", 0)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        mock_prepare.assert_called_once_with(e1, PROJECT)
        assert "placeholder_ts" not in delivered[0]
        assert "slack.thread_reply" in delivered[0]

    @patch("bobi.events.channels.SlackInputChannel.prepare")
    def test_drain_prepares_different_threads(self, mock_prepare, monkeypatch):
        """Events in different threads are prepared independently."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        e1 = _make_slack_event(thread_ts="171.42", ts="171.50")
        e2 = _make_slack_event(
            thread_ts="172.00", ts="172.01",
            conversation="slack:T123:channel:C123:thread:172.00")

        mock_prepare.side_effect = [e1, e2]

        q = SimpleQueue()
        q.put(e1)
        q.put(e2)

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(ev):
            lines = [f"Event: {ev['source']}/{ev['type']}"]
            for k, v in ev.get("fields", {}).items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: PROJECT)
        monkeypatch.setattr("bobi.events.drain.DRAIN_INTERVAL", 0)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        assert mock_prepare.call_count == 2

    @patch("bobi.events.channels.SlackInputChannel.prepare")
    def test_drain_handles_missing_project_root(self, mock_prepare,
                                                 monkeypatch):
        """A missing project root still delivers; the handler decides what
        to do with project_path=None."""
        from queue import SimpleQueue
        from bobi.events.drain import drain_loop

        event = _make_slack_event()
        mock_prepare.return_value = event

        q = SimpleQueue()
        q.put(event)

        delivered = []

        from bobi.inbox import register_local_inbox, unregister_local_inbox

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)
                raise SystemExit

        def fake_formatter(event):
            return f"Event: {event['source']}/{event['type']}"

        monkeypatch.setattr("bobi.events.drain._get_project_root", lambda: None)

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with pytest.raises(SystemExit):
                drain_loop("test-session", queue=q, formatter=fake_formatter)
        finally:
            unregister_local_inbox("test-session")

        mock_prepare.assert_called_once_with(event, None)
        assert len(delivered) == 1
