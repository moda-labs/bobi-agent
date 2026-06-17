"""Unit tests for the inbox module — no Claude sessions needed.

Tests the in-memory queue, the process-local inbox registry, and the
``deliver()`` publish path + transitional reply rendezvous. The inbox no
longer runs an HTTP server: messages arrive as ``inbox/<session>`` events on
the event server and are pushed into the queue by the drain loop.
"""

import os
import time
from unittest.mock import patch

import pytest

from modastack.inbox import (
    Inbox,
    Message,
    deliver,
    get_local_inbox,
    _msg_id,
    _reply_path,
    _write_reply,
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


class TestReplyRendezvous:
    """respond() writes a reply file the waiting sender polls (transitional)."""

    def test_respond_writes_reply_file(self, modastack_install):
        import json
        inbox = Inbox("test-resp")
        cid = _msg_id()
        inbox.respond(cid, "the answer")
        path = _reply_path(cid)
        assert path.exists()
        assert json.loads(path.read_text())["response"] == "the answer"

    def test_write_reply_is_atomic_overwrite(self, modastack_install):
        import json
        _write_reply("ab-cd", "first")
        _write_reply("ab-cd", "second")
        assert json.loads(_reply_path("ab-cd").read_text())["response"] == "second"

    def test_reply_path_rejects_traversal_ids(self, modastack_install):
        # corr_id is reconstructed from the wire payload — it must never be
        # usable to escape the replies dir.
        for bad in ["../../etc/cron", "..", "a/b", "x.json", "WHATEVER", ""]:
            with pytest.raises(ValueError):
                _reply_path(bad)

    def test_respond_swallows_unsafe_id_without_writing(self, modastack_install):
        # A malicious inbound id must not cause a write anywhere; respond()
        # logs and no-ops instead of traversing.
        from modastack import paths
        inbox = Inbox("test-evil")
        inbox.respond("../../../pwned", "secret response")
        assert not (paths.state_dir() / "replies" / "..").exists()
        # A real msg id still round-trips.
        mid = _msg_id()
        inbox.respond(mid, "ok")
        assert _reply_path(mid).exists()


def _register_live_session(name):
    from modastack.sdk import get_registry, SessionEntry
    get_registry().register(SessionEntry(name=name, cwd="/tmp", pid=os.getpid()))


class TestDeliver:
    """deliver() publishes inbox/<to> and preserves not-found/dead semantics."""

    def test_deliver_to_nonexistent_session(self, modastack_install):
        ok, resp = deliver("no-such-session", "hello")
        assert not ok
        assert "not found" in resp

    def test_deliver_to_dead_session(self, modastack_install):
        from modastack.sdk import get_registry, SessionEntry
        # A pid that is not alive.
        get_registry().register(SessionEntry(name="dead", cwd="/tmp", pid=2_000_000_000))
        ok, resp = deliver("dead", "hello")
        assert not ok
        assert "dead" in resp

    def test_deliver_rejects_terminal_status(self, modastack_install):
        # A live pid but a stopped session has torn down its inbox/subscription;
        # don't pretend it's reachable.
        from modastack.sdk import get_registry, SessionEntry
        get_registry().register(SessionEntry(
            name="gone", cwd="/tmp", pid=os.getpid(), status="stopped"))
        ok, resp = deliver("gone", "hello")
        assert not ok
        assert "stopped" in resp

    def test_nonblocking_deliver_publishes(self, modastack_install):
        _register_live_session("test-nb")
        with patch("modastack.events.publish.publish_inbox", return_value=True) as pub:
            ok, resp = deliver("test-nb", "hello", sender="cli", wait=False)
        assert ok and resp == ""
        pub.assert_called_once()
        to, payload = pub.call_args[0][0], pub.call_args[0][1]
        assert to == "test-nb"
        assert payload["text"] == "hello"
        assert payload["sender"] == "cli"
        assert payload["wait"] is False

    def test_deliver_reports_publish_failure(self, modastack_install):
        _register_live_session("test-fail")
        with patch("modastack.events.publish.publish_inbox", return_value=False):
            ok, resp = deliver("test-fail", "hello")
        assert not ok
        assert "could not publish" in resp

    def test_blocking_deliver_round_trips_via_reply_file(self, modastack_install):
        _register_live_session("test-block")

        # Simulate the target: as soon as the message is published, the target
        # session would process it and call inbox.respond(id, ...). Here we just
        # write the reply file for the published correlation id.
        def fake_publish(to, payload, project_path=None):
            _write_reply(payload["id"], "the answer")
            return True

        with patch("modastack.events.publish.publish_inbox", side_effect=fake_publish):
            ok, resp = deliver("test-block", "question?", wait=True, timeout=10)
        assert ok
        assert resp == "the answer"

    def test_blocking_deliver_times_out(self, modastack_install):
        _register_live_session("test-timeout")
        # publish succeeds but no reply ever lands.
        with patch("modastack.events.publish.publish_inbox", return_value=True):
            ok, resp = deliver("test-timeout", "question?", wait=True, timeout=1)
        assert not ok
        assert "no response" in resp

    def test_blocking_deliver_cleans_up_reply_file(self, modastack_install):
        _register_live_session("test-cleanup")
        captured = {}

        def fake_publish(to, payload, project_path=None):
            captured["id"] = payload["id"]
            _write_reply(payload["id"], "done")
            return True

        with patch("modastack.events.publish.publish_inbox", side_effect=fake_publish):
            deliver("test-cleanup", "q", wait=True, timeout=10)
        assert not _reply_path(captured["id"]).exists()
