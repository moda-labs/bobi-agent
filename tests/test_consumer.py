"""Tests for consumer drain loop — batching and inject."""

import time
import threading
from unittest.mock import patch, call
from queue import SimpleQueue

from bobi.events.client import format_event_for_manager
from bobi.inbox import register_local_inbox, unregister_local_inbox
from bobi import paths


class _CaptureInbox:
    """Stand-in inbox: captures pushed Messages; optionally stops the loop.

    The drain pushes directly into its session's in-process inbox (no HTTP).
    Raising SystemExit from push() after N messages breaks the otherwise
    blocking drain loop deterministically.
    """

    def __init__(self, stop_after=None):
        self.messages = []
        self.stop_after = stop_after

    def push(self, msg, priority=False):
        self.messages.append(msg)
        if self.stop_after and len(self.messages) >= self.stop_after:
            raise SystemExit()


class TestDrainLoop:

    def _make_event(self, source="github", etype="task.opened", text="",
                    delivery="bulk", **kwargs):
        data = {"issue_id": "1", "title": "Test", **kwargs}
        if text:
            data["text"] = text
        return {"type": etype, "source": source, "delivery": delivery,
                "data": data}

    def test_single_event_delivered(self):
        from bobi.events.client import event_queue
        from bobi.events.drain import drain_loop as _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event_queue.put(self._make_event())

        inbox = _CaptureInbox(stop_after=1)
        register_local_inbox("moda-mgr-test", inbox)
        try:
            _drain_loop("moda-mgr-test")
        except SystemExit:
            pass
        finally:
            unregister_local_inbox("moda-mgr-test")

        assert len(inbox.messages) == 1
        assert "Event: github/task.opened" in inbox.messages[0].text

    def test_drain_stops_on_poison_pill(self):
        """Subscription.stop() poison-pills the drain so its thread exits."""
        from bobi.events.drain import drain_loop as _drain_loop, _DRAIN_STOP

        q = SimpleQueue()
        register_local_inbox("stoppable", _CaptureInbox())
        t = threading.Thread(target=_drain_loop, args=("stoppable", q), daemon=True)
        t.start()
        try:
            q.put(_DRAIN_STOP)
            t.join(timeout=3)
            assert not t.is_alive()
        finally:
            unregister_local_inbox("stoppable")

    @patch("bobi.events.drain.DRAIN_INTERVAL", 0.1)
    def test_inbox_event_pushed_raw_and_skips_reactor(self):
        """inbox/* events are delivered raw (no formatting) and skip dispatch."""
        from bobi.events.drain import drain_loop as _drain_loop

        q = SimpleQueue()
        q.put({
            "source": "inbox",
            "type": "inbox/agent-x",
            "delivery": "bulk",
            "payload": {"id": "m1", "sender": "manager", "text": "ping you",
                        "wait": True},
        })

        reactor_calls = []
        reactor = type("R", (), {"process": staticmethod(
            lambda e: reactor_calls.append(e) or False)})()

        inbox = _CaptureInbox(stop_after=1)
        register_local_inbox("agent-x", inbox)
        try:
            _drain_loop("agent-x", queue=q, reactor=reactor)
        except SystemExit:
            pass
        finally:
            unregister_local_inbox("agent-x")

        assert len(inbox.messages) == 1
        msg = inbox.messages[0]
        assert msg.text == "ping you"          # raw, not "Event: inbox/..."
        assert msg.sender == "manager"
        assert msg.id == "m1" and msg.wait is True
        assert reactor_calls == []             # auto-dispatch skipped

    @patch("bobi.events.drain.DRAIN_INTERVAL", 0.1)
    def test_multiple_events_batched(self):
        from bobi.events.client import event_queue
        from bobi.events.drain import drain_loop as _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event_queue.put(self._make_event(etype="task.opened"))
        event_queue.put(self._make_event(etype="task.assigned"))
        event_queue.put(self._make_event(source="slack", etype="slack.dm",
                                          delivery="chat",
                                          text="hello", channel="D123", workspace="T123"))

        # Bulk group is pushed first, then the chat group — two pushes.
        inbox = _CaptureInbox(stop_after=2)
        register_local_inbox("moda-mgr-test", inbox)
        try:
            _drain_loop("moda-mgr-test")
        except SystemExit:
            pass
        finally:
            unregister_local_inbox("moda-mgr-test")

        assert len(inbox.messages) == 2
        github_text = inbox.messages[0].text
        slack_text = inbox.messages[1].text
        assert "task.opened" in github_text
        assert "task.assigned" in github_text
        assert "slack.dm" not in github_text
        assert "slack.dm" in slack_text


class TestBuildSubscriptions:

    def test_reads_from_agent_yaml(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "subscribe:\n  - github:org/repo\n  - slack:T123\n"
        )
        from bobi.events.subscriptions import discover_subscriptions
        subs = discover_subscriptions(tmp_path)
        assert "github:org/repo" in subs
        assert "slack:T123" in subs

    def test_fallback_to_dirname(self, tmp_path):
        from bobi.events.subscriptions import discover_subscriptions
        subs = discover_subscriptions(tmp_path)
        assert tmp_path.name in subs


class TestDrainLoopWithReactor:
    """Drain loop calls reactor.process() on each event before delivery."""

    def _make_review_event(self, number=42):
        return {
            "type": "github.pull_request_review",
            "source": "github",
            "delivery": "bulk",
            "topics": ["github:moda-labs/test"],
            "text": "[moda-labs/test] submitted PR #42 Fix bug (changes_requested)",
            "fields": {
                "action": "submitted",
                "number": number,
                "review_state": "changes_requested",
                "sender": "reviewer1",
            },
        }

    def _run_drain(self, event, reactor):
        from bobi.events.drain import drain_loop
        q = SimpleQueue()
        q.put(event)
        inbox = _CaptureInbox(stop_after=1)
        register_local_inbox("test-session", inbox)
        try:
            drain_loop("test-session", queue=q, reactor=reactor)
        except SystemExit:
            pass
        finally:
            unregister_local_inbox("test-session")
        return inbox

    def test_reactor_called_on_each_event(self):
        call_log = []

        def tracking_process(e):
            call_log.append(e)
            return False
        reactor = type("MockReactor", (), {"process": staticmethod(tracking_process)})()

        self._run_drain(self._make_review_event(), reactor)

        assert len(call_log) == 1
        assert call_log[0]["type"] == "github.pull_request_review"

    def test_auto_dispatched_event_annotated(self):
        """Events auto-dispatched by reactor get an annotation in the pushed text."""
        reactor = type("MockReactor", (), {"process": lambda self, e: "dispatched"})()
        inbox = self._run_drain(self._make_review_event(), reactor)
        text = inbox.messages[0].text
        assert "[auto-dispatched:" in text.lower() or \
               "[AUTO-DISPATCH" in text

    def test_non_matching_event_not_annotated(self):
        """Events that don't match any rule pass through without annotation."""
        reactor = type("MockReactor", (), {"process": lambda self, e: None})()
        event = {"type": "github.issues", "source": "github", "delivery": "bulk",
                 "fields": {"action": "opened"}}
        inbox = self._run_drain(event, reactor)
        assert "AUTO-DISPATCH" not in inbox.messages[0].text


class TestCursorAckAfterDelivery:
    """cursor_ack fires only after the session PROCESSES the pushed message
    (#278, #688) - never at push time, so a restart replays queued messages.
    Deeper coverage (watermark, out-of-order completion) in test_drain_ack.py."""

    def test_cursor_ack_called_with_max_seq_after_processing(self):
        from bobi.events.drain import drain_loop, _DRAIN_STOP
        q = SimpleQueue()
        q.put({"type": "push", "source": "github", "delivery": "bulk",
               "seq": 5, "data": {"issue_id": "1"}})
        q.put({"type": "pr", "source": "github", "delivery": "bulk",
               "seq": 7, "data": {"issue_id": "2"}})
        q.put(_DRAIN_STOP)

        acked = []
        inbox = _CaptureInbox()
        register_local_inbox("test-ack", inbox)
        try:
            drain_loop("test-ack", queue=q,
                       cursor_ack=lambda seq: acked.append(seq))
        finally:
            unregister_local_inbox("test-ack")

        # Both events delivered in one batch as one message; nothing acked
        # until that message is processed, then the batch max seq is.
        assert len(inbox.messages) == 1
        assert acked == []
        inbox.messages[0].on_done()
        assert acked == [7]

    def test_cursor_ack_not_called_for_zero_seq(self):
        from bobi.events.drain import drain_loop, _DRAIN_STOP
        q = SimpleQueue()
        q.put({"type": "push", "source": "github", "delivery": "bulk",
               "data": {"issue_id": "1"}})  # no seq field
        q.put(_DRAIN_STOP)

        acked = []
        inbox = _CaptureInbox()
        register_local_inbox("test-ack-zero", inbox)
        try:
            drain_loop("test-ack-zero", queue=q,
                       cursor_ack=lambda seq: acked.append(seq))
        finally:
            unregister_local_inbox("test-ack-zero")

        # An unsequenced batch has no cursor to advance - no ack wiring.
        assert inbox.messages[0].on_done is None
        assert acked == []


class TestFormatBatching:

    def test_multiple_events_joined(self):
        events = [
            {"type": "task.opened", "source": "github",
             "data": {"issue_id": "1", "title": "First"}},
            {"type": "slack.dm", "source": "slack",
             "data": {"from": "Zach", "text": "hello", "channel": "D123",
                      "workspace": "T123"}},
        ]
        lines = [format_event_for_manager(e) for e in events]
        text = "\n\n".join(lines)
        assert text.count("Event:") == 2
        assert "task.opened" in text
        assert "slack.dm" in text
