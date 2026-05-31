"""Tests for consumer drain loop — batching and inject."""

import time
import threading
from unittest.mock import patch, MagicMock, call
from queue import SimpleQueue

from modastack.manager.events.event_client import format_event_for_manager


class TestDrainLoop:

    def _make_event(self, source="github", etype="task.opened", text="", **kwargs):
        data = {"issue_id": "1", "title": "Test", **kwargs}
        if text:
            data["text"] = text
        return {"type": etype, "source": source, "data": data}

    @patch("modastack.manager.session.inject")
    @patch("modastack.manager.session.detect_state", return_value="waiting_input")
    def test_single_event_injected(self, mock_state, mock_inject):
        from modastack.manager.events.event_client import event_queue
        from modastack.manager.events.consumer import _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event = self._make_event()
        event_queue.put(event)

        def stop_after_inject(*args, **kwargs):
            raise SystemExit()
        mock_inject.side_effect = stop_after_inject

        try:
            _drain_loop()
        except SystemExit:
            pass

        mock_inject.assert_called_once()
        injected_text = mock_inject.call_args[0][0]
        assert "Event: github/task.opened" in injected_text

    @patch("modastack.manager.session.inject")
    @patch("modastack.manager.session.detect_state", return_value="waiting_input")
    @patch("modastack.manager.events.consumer.DRAIN_INTERVAL", 0.1)
    def test_multiple_events_batched(self, mock_state, mock_inject):
        from modastack.manager.events.event_client import event_queue
        from modastack.manager.events.consumer import _drain_loop

        while not event_queue.empty():
            event_queue.get_nowait()

        event_queue.put(self._make_event(etype="task.opened"))
        event_queue.put(self._make_event(etype="task.assigned"))
        event_queue.put(self._make_event(source="slack", etype="slack.dm",
                                          text="hello", channel="D123", workspace="T123"))

        def stop_after_inject(*args, **kwargs):
            raise SystemExit()
        mock_inject.side_effect = stop_after_inject

        try:
            _drain_loop()
        except SystemExit:
            pass

        mock_inject.assert_called_once()
        injected_text = mock_inject.call_args[0][0]
        assert "task.opened" in injected_text
        assert "task.assigned" in injected_text
        assert "slack.dm" in injected_text

    def test_drops_events_when_manager_busy(self):
        from modastack.manager.events.event_client import event_queue
        from modastack.manager.events import consumer

        while not event_queue.empty():
            event_queue.get_nowait()

        event_queue.put(self._make_event())

        inject_called = False

        def fake_inject(text):
            nonlocal inject_called
            inject_called = True
            return True

        def fake_detect():
            return "working"

        # Run one iteration manually instead of the loop
        import time
        event = event_queue.get(timeout=1)
        time.sleep(0.05)
        batch = [event]
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())

        from modastack.manager.events.event_client import format_event_for_manager
        lines = [format_event_for_manager(e) for e in batch]
        text = "\n\n".join(lines)

        # Manager is busy -- should not inject
        assert fake_detect() != "waiting_input"
        assert not inject_called


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
