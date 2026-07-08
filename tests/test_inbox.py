"""Unit tests for the inbox module — no Claude sessions needed.

Tests the in-memory queue, the process-local inbox registry, and the
``deliver()`` publish path + async request/reply (#269). The inbox no longer
runs an HTTP server: messages arrive as ``inbox/<session>`` events on the
event server and are pushed into the queue by the drain loop. A blocking
``deliver(wait=True)`` subscribes to a transient ``reply/<uuid>`` topic and
matches the reply on its correlation id; the target replies by publishing to
that topic (``Inbox.respond``).
"""

import os
import queue
import time
from unittest.mock import patch

from bobi.inbox import (
    Inbox,
    Message,
    deliver,
    get_local_inbox,
    _await_reply,
    _msg_id,
)


class TestMessageId:
    def test_ids_are_unique(self):
        ids = {_msg_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_are_sortable(self):
        a = _msg_id()
        time.sleep(0.01)
        b = _msg_id()
        assert a < b


class TestInboxQueue:
    """The inbox is a plain in-process queue its run loop drains."""

    def test_recv_returns_none_on_timeout(self):
        inbox = Inbox("test-empty")
        assert inbox.recv(timeout=0.1) is None
        inbox.close()

    def test_push_and_recv(self):
        inbox = Inbox("test-push")
        inbox.push(Message(id="1", sender="s", text="hello"))
        msg = inbox.recv(timeout=1)
        assert msg is not None and msg.text == "hello"
        inbox.close()

    def test_multiple_messages_in_order(self):
        inbox = Inbox("test-order")
        for i in range(5):
            inbox.push(Message(id=str(i), sender="s", text=f"msg-{i}"))
        received = []
        while (m := inbox.recv(timeout=0.2)) is not None:
            received.append(m.text)
        assert received == [f"msg-{i}" for i in range(5)]
        inbox.close()

    def test_priority_messages_are_received_before_normal_messages(self):
        inbox = Inbox("test-priority")
        inbox.push(Message(id="bulk-1", sender="event-bus", text="bulk 1"))
        inbox.push(Message(id="chat-1", sender="event-bus", text="chat 1"), priority=True)
        inbox.push(Message(id="bulk-2", sender="event-bus", text="bulk 2"))
        inbox.push(Message(id="chat-2", sender="event-bus", text="chat 2"), priority=True)

        received = []
        while (m := inbox.recv(timeout=0.2)) is not None:
            received.append(m.text)

        assert received == ["chat 1", "chat 2", "bulk 1", "bulk 2"]
        inbox.close()


class TestLocalInboxRegistry:
    """start()/close() make a session addressable in-process for its drain."""

    def test_start_registers_and_close_unregisters(self):
        inbox = Inbox("test-reg")
        inbox.start()
        assert get_local_inbox("test-reg") is inbox
        inbox.close()
        assert get_local_inbox("test-reg") is None

    def test_get_unknown_returns_none(self):
        assert get_local_inbox("never-registered") is None


class TestRespond:
    """respond() publishes the reply to the message's reply_to topic."""

    def test_respond_publishes_to_reply_to(self):
        inbox = Inbox("test-resp")
        msg = Message(id="cid-1", sender="x", text="q", wait=True,
                      reply_to="reply/abc")
        with patch("bobi.events.publish.publish_reply",
                   return_value=True) as pub:
            inbox.respond(msg, "the answer")
        pub.assert_called_once_with("reply/abc", "cid-1", "the answer")

    def test_respond_noops_without_reply_to(self):
        # A fire-and-forget message has no reply channel — respond must not
        # try to publish anywhere.
        inbox = Inbox("test-resp-noop")
        msg = Message(id="cid-2", sender="x", text="q", wait=False)
        with patch("bobi.events.publish.publish_reply") as pub:
            inbox.respond(msg, "ignored")
        pub.assert_not_called()

    def test_respond_rejects_non_reply_topic(self):
        # reply_to is wire input — a crafted request must not be able to
        # redirect the agent's response into an arbitrary topic (e.g. another
        # session's inbox). Only reply/ topics are honored.
        inbox = Inbox("test-resp-evil")
        msg = Message(id="cid-3", sender="x", text="q", wait=True,
                      reply_to="inbox/victim")
        with patch("bobi.events.publish.publish_reply") as pub:
            inbox.respond(msg, "secret agent output")
        pub.assert_not_called()


class TestAwaitReply:
    """_await_reply correlates strictly on corr_id (no crossed replies)."""

    def _chan(self):
        class _FakeChannel:
            def __init__(self):
                self.queue = queue.SimpleQueue()
        return _FakeChannel()

    def test_returns_matching_reply(self):
        chan = self._chan()
        chan.queue.put({"payload": {"corr_id": "mine", "response": "for me"}})
        ok, resp = _await_reply(chan, "mine", time.monotonic() + 5, 5)
        assert ok and resp == "for me"

    def test_ignores_mismatched_corr_id(self):
        # A reply for a different in-flight ask must be skipped, not returned.
        chan = self._chan()
        chan.queue.put({"payload": {"corr_id": "other", "response": "not mine"}})
        chan.queue.put({"payload": {"corr_id": "mine", "response": "for me"}})
        ok, resp = _await_reply(chan, "mine", time.monotonic() + 5, 5)
        assert ok and resp == "for me"

    def test_times_out_when_no_reply(self):
        chan = self._chan()
        ok, resp = _await_reply(chan, "mine", time.monotonic() + 1, 1)
        assert not ok and "no response within 1s" in resp


def _register_live_session(name):
    from bobi.sdk import get_registry, SessionEntry
    get_registry().register(SessionEntry(name=name, cwd="/tmp", pid=os.getpid()))


class _FakeChannel:
    """Stand-in for a transient reply subscription in deliver() unit tests."""

    def __init__(self, connected=True):
        self.queue = queue.SimpleQueue()
        self.topic = "reply/unit-test"
        self.closed = False
        self._connected = connected

    def wait_connected(self, timeout):
        return self._connected

    def close(self):
        self.closed = True


class TestDeliver:
    """deliver() publishes inbox/<to> and preserves not-found/dead semantics."""

    def test_deliver_to_nonexistent_session(self, bobi_install):
        ok, resp = deliver("no-such-session", "hello")
        assert not ok
        assert "not found" in resp

    def test_deliver_to_dead_session(self, bobi_install):
        from bobi.sdk import get_registry, SessionEntry
        # A pid that is not alive.
        get_registry().register(SessionEntry(name="dead", cwd="/tmp", pid=2_000_000_000))
        ok, resp = deliver("dead", "hello")
        assert not ok
        assert "dead" in resp

    def test_deliver_rejects_terminal_status(self, bobi_install):
        # A live pid but a stopped session has torn down its inbox/subscription;
        # don't pretend it's reachable.
        from bobi.sdk import get_registry, SessionEntry
        get_registry().register(SessionEntry(
            name="gone", cwd="/tmp", pid=os.getpid(), status="stopped"))
        ok, resp = deliver("gone", "hello")
        assert not ok
        assert "stopped" in resp

    def test_nonblocking_deliver_publishes(self, bobi_install):
        _register_live_session("test-nb")
        with patch("bobi.events.publish.publish_inbox", return_value=True) as pub:
            ok, resp = deliver("test-nb", "hello", sender="cli", wait=False)
        assert ok and resp == ""
        pub.assert_called_once()
        to, payload = pub.call_args[0][0], pub.call_args[0][1]
        assert to == "test-nb"
        assert payload["text"] == "hello"
        assert payload["sender"] == "cli"
        assert payload["wait"] is False

    def test_deliver_reports_publish_failure(self, bobi_install):
        _register_live_session("test-fail")
        with patch("bobi.events.publish.publish_inbox", return_value=False):
            ok, resp = deliver("test-fail", "hello")
        assert not ok
        assert "could not publish" in resp

    def test_blocking_deliver_round_trips_via_reply_topic(self, bobi_install):
        _register_live_session("test-block")
        chan = _FakeChannel()

        # Stand in for the target: as soon as the request is published, the
        # target session would reply on the request's reply_to topic. Here we
        # push the correlated reply straight onto the channel's queue.
        def fake_publish(to, payload, project_path=None):
            assert payload["reply_to"] == chan.topic
            chan.queue.put({"payload": {"corr_id": payload["id"],
                                        "response": "the answer"}})
            return True

        with patch("bobi.inbox._open_reply_channel", return_value=chan), \
             patch("bobi.events.publish.publish_inbox", side_effect=fake_publish):
            ok, resp = deliver("test-block", "question?", wait=True, timeout=10)
        assert ok
        assert resp == "the answer"
        assert chan.closed  # transient subscription torn down

    def test_blocking_deliver_times_out(self, bobi_install):
        _register_live_session("test-timeout")
        chan = _FakeChannel()
        # publish succeeds but no reply ever lands on the channel.
        with patch("bobi.inbox._open_reply_channel", return_value=chan), \
             patch("bobi.events.publish.publish_inbox", return_value=True):
            ok, resp = deliver("test-timeout", "question?", wait=True, timeout=1)
        assert not ok
        assert "no response" in resp
        assert chan.closed

    def test_blocking_deliver_reports_channel_failure(self, bobi_install):
        # If the reply channel can't be opened (event server unreachable),
        # a blocking deliver fails fast rather than hanging.
        _register_live_session("test-nochan")
        with patch("bobi.inbox._open_reply_channel", return_value=None):
            ok, resp = deliver("test-nochan", "q", wait=True, timeout=1)
        assert not ok
        assert "could not publish" in resp

    def test_blocking_deliver_does_not_publish_until_connected(self, bobi_install):
        # Subscribe-before-publish: if the reply subscription never connects,
        # deliver must NOT publish the request (the reply would be lost) and
        # must report a timeout instead of hanging.
        _register_live_session("test-noconnect")
        chan = _FakeChannel(connected=False)
        with patch("bobi.inbox._open_reply_channel", return_value=chan), \
             patch("bobi.events.publish.publish_inbox") as pub:
            ok, resp = deliver("test-noconnect", "q", wait=True, timeout=1)
        assert not ok
        assert "no response" in resp
        pub.assert_not_called()  # never published without a live subscription
        assert chan.closed
