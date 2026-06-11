"""Tests for consumer drain loop — batching and inject."""

import time
import threading
from unittest.mock import patch, call
from queue import SimpleQueue

from modastack.events.client import format_event_for_manager


class TestDrainLoop:

    def _make_event(self, source="github", etype="task.opened", text="",
                    delivery="bulk", **kwargs):
        data = {"issue_id": "1", "title": "Test", **kwargs}
        if text:
            data["text"] = text
        return {"type": etype, "source": source, "delivery": delivery,
                "data": data}

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_single_event_delivered(self, mock_deliver):
        from modastack.events.client import event_queue
        from modastack.events.drain import drain_loop as _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event = self._make_event()
        event_queue.put(event)

        def stop_after_deliver(*args, **kwargs):
            raise SystemExit()
        mock_deliver.side_effect = stop_after_deliver

        try:
            _drain_loop("moda-mgr-test")
        except SystemExit:
            pass

        mock_deliver.assert_called_once()
        delivered_to = mock_deliver.call_args[0][0]
        delivered_text = mock_deliver.call_args[0][1]
        assert delivered_to == "moda-mgr-test"
        assert "Event: github/task.opened" in delivered_text

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    @patch("modastack.events.drain.DRAIN_INTERVAL", 0.1)
    def test_multiple_events_batched(self, mock_deliver):
        from modastack.events.client import event_queue
        from modastack.events.drain import drain_loop as _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event_queue.put(self._make_event(etype="task.opened"))
        event_queue.put(self._make_event(etype="task.assigned"))
        event_queue.put(self._make_event(source="slack", etype="slack.dm",
                                          delivery="chat",
                                          text="hello", channel="D123", workspace="T123"))

        call_count = 0
        def stop_after_slack(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise SystemExit()
            return True, ""
        mock_deliver.side_effect = stop_after_slack

        try:
            _drain_loop("moda-mgr-test")
        except SystemExit:
            pass

        assert mock_deliver.call_count == 2
        github_text = mock_deliver.call_args_list[0][0][1]
        slack_text = mock_deliver.call_args_list[1][0][1]
        assert "task.opened" in github_text
        assert "task.assigned" in github_text
        assert "slack.dm" not in github_text
        assert "slack.dm" in slack_text


class TestBuildSubscriptions:

    def test_reads_from_agent_yaml(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text(
            "subscribe:\n  - github:org/repo\n  - slack:T123\n"
        )
        from modastack.events.subscriptions import discover_subscriptions
        subs = discover_subscriptions(tmp_path)
        assert "github:org/repo" in subs
        assert "slack:T123" in subs

    def test_fallback_to_dirname(self, tmp_path):
        from modastack.events.subscriptions import discover_subscriptions
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

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_reactor_called_on_each_event(self, mock_deliver):
        from modastack.events.drain import drain_loop
        q = SimpleQueue()
        reactor = type("MockReactor", (), {"process": lambda self, e: False})()

        event = self._make_review_event()
        q.put(event)

        call_log = []
        orig_process = reactor.process
        def tracking_process(e):
            call_log.append(e)
            return False
        reactor.process = tracking_process

        mock_deliver.side_effect = lambda *a, **kw: (_ for _ in ()).throw(SystemExit())

        try:
            drain_loop("test-session", queue=q, reactor=reactor)
        except SystemExit:
            pass

        assert len(call_log) == 1
        assert call_log[0]["type"] == "github.pull_request_review"

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_auto_dispatched_event_annotated(self, mock_deliver):
        """Events auto-dispatched by reactor get an annotation in the formatted text."""
        from modastack.events.drain import drain_loop
        q = SimpleQueue()
        # Reactor that claims it dispatched
        reactor = type("MockReactor", (), {"process": lambda self, e: True})()

        event = self._make_review_event()
        q.put(event)

        mock_deliver.side_effect = lambda *a, **kw: (_ for _ in ()).throw(SystemExit())

        try:
            drain_loop("test-session", queue=q, reactor=reactor)
        except SystemExit:
            pass

        delivered_text = mock_deliver.call_args[0][1]
        assert "[auto-dispatched: pr-feedback workflow launched]" in delivered_text.lower() or \
               "[AUTO-DISPATCH" in delivered_text

    @patch("modastack.inbox.deliver", return_value=(True, ""))
    def test_non_matching_event_not_annotated(self, mock_deliver):
        """Events that don't match any rule pass through without annotation."""
        from modastack.events.drain import drain_loop
        q = SimpleQueue()
        reactor = type("MockReactor", (), {"process": lambda self, e: False})()

        event = {"type": "github.issues", "source": "github", "delivery": "bulk",
                 "fields": {"action": "opened"}}
        q.put(event)

        mock_deliver.side_effect = lambda *a, **kw: (_ for _ in ()).throw(SystemExit())

        try:
            drain_loop("test-session", queue=q, reactor=reactor)
        except SystemExit:
            pass

        delivered_text = mock_deliver.call_args[0][1]
        assert "AUTO-DISPATCH" not in delivered_text


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
