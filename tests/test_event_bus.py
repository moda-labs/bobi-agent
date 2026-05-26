"""Tests for the in-process event bus."""

import threading
import time
from unittest.mock import patch

from modastack.manager.events.bus import EventBus


class TestEventBus:

    def test_push_and_drain(self):
        bus = EventBus()
        bus.push("test.event", "test", {"key": "value"})
        events = bus.drain()
        assert len(events) == 1
        assert events[0]["type"] == "test.event"
        assert events[0]["source"] == "test"
        assert events[0]["data"] == {"key": "value"}
        assert "timestamp" in events[0]

    def test_drain_clears_queue(self):
        bus = EventBus()
        bus.push("a", "src", {})
        bus.push("b", "src", {})
        events = bus.drain()
        assert len(events) == 2
        assert bus.drain() == []

    def test_pending_count(self):
        bus = EventBus()
        assert bus.pending() == 0
        bus.push("a", "src", {})
        bus.push("b", "src", {})
        assert bus.pending() == 2
        bus.drain()
        assert bus.pending() == 0

    def test_max_size_evicts_oldest(self):
        bus = EventBus(max_size=3)
        for i in range(5):
            bus.push(f"event-{i}", "src", {})
        events = bus.drain()
        assert len(events) == 3
        assert events[0]["type"] == "event-2"
        assert events[2]["type"] == "event-4"

    def test_wait_returns_true_when_event_pushed(self):
        bus = EventBus()
        bus.push("a", "src", {})
        assert bus.wait(timeout=0.01) is True

    def test_wait_returns_false_on_timeout(self):
        bus = EventBus()
        assert bus.wait(timeout=0.01) is False

    def test_wait_unblocks_on_push(self):
        bus = EventBus()
        result = []

        def pusher():
            time.sleep(0.05)
            bus.push("late", "src", {})

        t = threading.Thread(target=pusher)
        t.start()
        got = bus.wait(timeout=1.0)
        t.join()
        assert got is True
        assert bus.pending() == 1

    def test_thread_safety(self):
        bus = EventBus()
        errors = []

        def push_events(n):
            try:
                for i in range(n):
                    bus.push(f"event-{i}", "thread", {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=push_events, args=(50,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        events = bus.drain()
        assert len(events) == 250

    @patch("modastack.manager.events.bus.EVENT_LOG")
    def test_push_appends_to_event_log(self, mock_path, tmp_path):
        log_file = tmp_path / "events.jsonl"
        mock_path.__truediv__ = lambda self, x: log_file
        mock_path.parent = tmp_path

        bus = EventBus()
        # Patch EVENT_LOG directly for the write
        import modastack.manager.events.bus as bus_mod
        original = bus_mod.EVENT_LOG
        bus_mod.EVENT_LOG = log_file
        try:
            bus.push("test", "src", {"x": 1})
            assert log_file.exists()
            import json
            entry = json.loads(log_file.read_text().strip())
            assert entry["type"] == "test"
        finally:
            bus_mod.EVENT_LOG = original
