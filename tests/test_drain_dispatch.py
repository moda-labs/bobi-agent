"""Tests for drain loop integration with auto-dispatch via EventReactor."""

import queue
import time
from unittest.mock import patch, MagicMock

from modastack.events.drain import drain_loop
from modastack.events.reactor import AutoDispatchRule, EventReactor


def _wait_calls(mock, n, timeout=2.0):
    """Auto-dispatch offloads launch_agent to a daemon thread; wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock.call_count >= n:
            return
        time.sleep(0.005)


class _OneShotQueue:
    """A queue that yields pre-loaded events then raises to stop the loop."""

    def __init__(self, events):
        self._events = list(events)
        self._calls = 0

    def get(self):
        self._calls += 1
        if self._calls == 1 and self._events:
            return self._events[0]
        raise KeyboardInterrupt

    def empty(self):
        if self._calls == 1 and len(self._events) > 1:
            return False
        return True

    def get_nowait(self):
        if len(self._events) > 1:
            return self._events.pop(1)
        raise queue.Empty


class TestDrainAutoDispatch:
    """drain_loop auto-dispatches matching events before delivery."""

    def _make_reactor(self):
        rules = [
            AutoDispatchRule(
                event="github.pull_request_review",
                workflow="pr-feedback",
                match={"review_state": "changes_requested"},
                cooldown=0,
            ),
        ]
        return EventReactor(rules=rules, cwd="/tmp/proj")

    def _run_drain_one_batch(self, events, reactor=None):
        """Run drain_loop for exactly one batch and capture pushed messages."""
        from modastack.inbox import register_local_inbox, unregister_local_inbox

        q = _OneShotQueue(events)
        delivered = []

        class _CaptureInbox:
            def push(self, msg):
                delivered.append(msg.text)

        def fake_formatter(event):
            return event.get("text", "")

        register_local_inbox("test-session", _CaptureInbox())
        try:
            with patch("modastack.events.drain.time.sleep"):
                try:
                    drain_loop("test-session", queue=q,
                               formatter=fake_formatter,
                               reactor=reactor)
                except KeyboardInterrupt:
                    pass
        finally:
            unregister_local_inbox("test-session")

        return delivered

    @patch("modastack.subagent.launch_agent")
    def test_matching_event_gets_annotation(self, mock_launch):
        """Auto-dispatched events get annotation appended to text."""
        mock_launch.return_value = "wf-pr-feedback-test-1"
        reactor = self._make_reactor()

        events = [{
            "type": "github.pull_request_review",
            "text": "[org/repo] submitted PR #1",
            "delivery": "bulk",
            "topics": ["github:org/repo"],
            "fields": {"review_state": "changes_requested", "number": 1},
        }]

        delivered = self._run_drain_one_batch(events, reactor=reactor)

        assert len(delivered) == 1
        assert "AUTO-DISPATCHED" in delivered[0]
        assert "[org/repo] submitted PR #1" in delivered[0]
        _wait_calls(mock_launch, 1)
        mock_launch.assert_called_once()

    @patch("modastack.subagent.launch_agent")
    def test_non_matching_event_passes_through(self, mock_launch):
        """Non-matching events are delivered without annotation."""
        reactor = self._make_reactor()

        events = [{
            "type": "github.issues",
            "text": "[org/repo] opened issue #5",
            "delivery": "bulk",
            "fields": {"action": "opened"},
        }]

        delivered = self._run_drain_one_batch(events, reactor=reactor)

        assert len(delivered) == 1
        assert "AUTO-DISPATCHED" not in delivered[0]
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_suppressed_event_gets_suppressed_annotation(self, mock_launch):
        """Suppressed events get a SUPPRESSED annotation, not AUTO-DISPATCHED."""
        rules = [
            AutoDispatchRule(
                event="github.pull_request",
                workflow="",
                match={"action": "review_requested"},
                suppress=True,
                cooldown=0,
            ),
        ]
        reactor = EventReactor(rules=rules, cwd="/tmp/proj")

        events = [{
            "type": "github.pull_request",
            "text": "[org/repo] review_requested PR #99",
            "delivery": "bulk",
            "topics": ["github:org/repo"],
            "fields": {"action": "review_requested", "number": 99},
        }]

        delivered = self._run_drain_one_batch(events, reactor=reactor)

        assert len(delivered) == 1
        assert "SUPPRESSED" in delivered[0]
        assert "no action needed" in delivered[0]
        assert "AUTO-DISPATCHED" not in delivered[0]
        mock_launch.assert_not_called()

    def test_no_dispatch_when_reactor_is_none(self):
        """When reactor is None, auto-dispatch is disabled entirely."""
        events = [{
            "type": "github.pull_request_review",
            "text": "[org/repo] submitted PR #1",
            "delivery": "bulk",
            "fields": {"review_state": "changes_requested"},
        }]

        delivered = self._run_drain_one_batch(events, reactor=None)

        assert len(delivered) == 1
        assert "AUTO-DISPATCHED" not in delivered[0]
