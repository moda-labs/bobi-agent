"""Unit tests for the inbox module — no Claude sessions needed.

Tests the in-memory queue, HTTP server, deliver() function, and
blocking/non-blocking message modes.
"""

import threading
import time

import pytest

from modastack.inbox import Inbox, Message, deliver, _msg_id


class TestMessageId:
    def test_ids_are_unique(self):
        ids = {_msg_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_are_sortable(self):
        a = _msg_id()
        time.sleep(0.01)
        b = _msg_id()
        assert a < b


class TestInboxDirect:
    """Test the Inbox queue directly (no HTTP)."""

    def test_recv_returns_none_on_timeout(self):
        inbox = Inbox("test-empty")
        msg = inbox.recv(timeout=0.1)
        assert msg is None
        inbox.close()

    def test_put_and_recv(self):
        inbox = Inbox("test-put")
        inbox._queue.put(Message(id="1", sender="s", text="hello"))
        msg = inbox.recv(timeout=1)
        assert msg is not None
        assert msg.text == "hello"
        inbox.close()

    def test_close_unblocks_pending_asks(self, modastack_install):
        inbox = Inbox("test-close")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-close", inbox_port=inbox.port, cwd="/tmp",
        ))

        results = []

        def sender():
            ok, resp = deliver("test-close", "question?", wait=True, timeout=10)
            results.append((ok, resp))

        t = threading.Thread(target=sender)
        t.start()

        time.sleep(0.3)
        inbox.close()
        t.join(timeout=5)

        assert len(results) == 1
        ok, resp = results[0]
        assert not ok
        assert "closed" in resp or "cannot reach" in resp


class TestInboxHTTP:
    """Test delivery via the HTTP server."""

    def test_start_assigns_port(self):
        inbox = Inbox("test-port")
        port = inbox.start()
        assert port > 0
        assert inbox.port == port
        inbox.close()

    def test_nonblocking_deliver(self, modastack_install):
        inbox = Inbox("test-nb")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-nb", inbox_port=inbox.port, cwd="/tmp",
        ))

        ok, resp = deliver("test-nb", "hello", wait=False)
        assert ok
        assert resp == ""

        msg = inbox.recv(timeout=2)
        assert msg is not None
        assert msg.text == "hello"

        inbox.close()

    def test_blocking_deliver(self, modastack_install):
        inbox = Inbox("test-block")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-block", inbox_port=inbox.port, cwd="/tmp",
        ))

        results = []

        def sender():
            ok, resp = deliver("test-block", "question?", wait=True, timeout=10)
            results.append((ok, resp))

        t = threading.Thread(target=sender)
        t.start()

        msg = inbox.recv(timeout=5)
        assert msg is not None
        assert msg.wait is True
        assert msg.text == "question?"

        inbox.respond(msg.id, "answer!")
        t.join(timeout=5)

        assert len(results) == 1
        ok, resp = results[0]
        assert ok
        assert resp == "answer!"

        inbox.close()

    def test_blocking_deliver_timeout(self, modastack_install):
        inbox = Inbox("test-timeout")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-timeout", inbox_port=inbox.port, cwd="/tmp",
        ))

        ok, resp = deliver("test-timeout", "question?", wait=True, timeout=1)
        assert not ok
        assert "no response" in resp

        inbox.close()

    def test_deliver_to_nonexistent_session(self, modastack_install):
        ok, resp = deliver("no-such-session", "hello")
        assert not ok
        assert "not found" in resp

    def test_deliver_to_session_without_inbox(self, modastack_install):
        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-no-inbox", cwd="/tmp", inbox_port=0,
        ))

        ok, resp = deliver("test-no-inbox", "hello")
        assert not ok
        assert "no inbox" in resp

    def test_multiple_messages_in_order(self, modastack_install):
        inbox = Inbox("test-order")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-order", inbox_port=inbox.port, cwd="/tmp",
        ))

        for i in range(5):
            ok, _ = deliver("test-order", f"msg-{i}", wait=False)
            assert ok

        time.sleep(0.2)

        received = []
        while True:
            msg = inbox.recv(timeout=0.5)
            if msg is None:
                break
            received.append(msg.text)

        assert received == [f"msg-{i}" for i in range(5)]
        inbox.close()

    def test_health_endpoint(self):
        import json
        import urllib.request

        inbox = Inbox("test-health")
        inbox.start()

        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{inbox.port}/health", timeout=5
        )
        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["session"] == "test-health"

        inbox.close()

    def test_concurrent_blocking_delivers(self, modastack_install):
        inbox = Inbox("test-concurrent")
        inbox.start()

        from modastack.sdk import get_registry, SessionEntry
        registry = get_registry()
        registry.register(SessionEntry(
            name="test-concurrent", inbox_port=inbox.port, cwd="/tmp",
        ))

        results = {}

        def sender(label):
            ok, resp = deliver("test-concurrent", f"q-{label}", wait=True, timeout=10)
            results[label] = (ok, resp)

        threads = [threading.Thread(target=sender, args=(f"t{i}",)) for i in range(3)]
        for t in threads:
            t.start()

        time.sleep(0.3)

        for _ in range(3):
            msg = inbox.recv(timeout=5)
            assert msg is not None
            inbox.respond(msg.id, f"a-{msg.text}")

        for t in threads:
            t.join(timeout=5)

        assert len(results) == 3
        for label, (ok, resp) in results.items():
            assert ok
            assert resp == f"a-q-{label}"

        inbox.close()
