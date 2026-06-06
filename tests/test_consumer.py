"""Tests for consumer drain loop — batching and inject."""

import time
import threading
from unittest.mock import patch, call
from queue import SimpleQueue

from modastack.events.client import format_event_for_manager


class TestDrainLoop:

    def _make_event(self, source="github", etype="task.opened", text="", **kwargs):
        data = {"issue_id": "1", "title": "Test", **kwargs}
        if text:
            data["text"] = text
        return {"type": etype, "source": source, "data": data}

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

    def test_slack_workspace(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("slack:\n  workspace_id: T123\n")
        from modastack.events.subscriptions import build_subscriptions as _build_subscriptions
        subs = _build_subscriptions(tmp_path)
        assert "slack:T123" in subs

    def test_no_slack_config(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}\n")
        from modastack.events.subscriptions import build_subscriptions as _build_subscriptions
        subs = _build_subscriptions(tmp_path)
        assert not any("slack:" in s for s in subs)


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
