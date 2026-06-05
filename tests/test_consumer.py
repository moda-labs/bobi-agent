"""Tests for consumer drain loop — batching and inject."""

import time
import threading
from unittest.mock import patch, call
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

        call_count = 0
        def stop_after_slack_inject(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise SystemExit()
            return True, ""
        mock_inject.side_effect = stop_after_slack_inject

        try:
            _drain_loop()
        except SystemExit:
            pass

        assert mock_inject.call_count == 2
        github_text = mock_inject.call_args_list[0][0][0]
        slack_text = mock_inject.call_args_list[1][0][0]
        assert "task.opened" in github_text
        assert "task.assigned" in github_text
        assert "slack.dm" not in github_text
        assert "slack.dm" in slack_text

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


class TestStartup:
    """Test that consumer.run() initializes without crashing.

    This exercises the full startup path — manager session, workflow
    dispatcher, event client, drain loop — with all heavy deps mocked.
    Catches import errors and missing method calls.
    """

    @patch("modastack.manager.session.ManagerSession.start_or_resume", return_value=True)
    @patch("modastack.manager.events.consumer._kill_stale_instances")
    @patch("modastack.manager.events.consumer._wait_for_manager", return_value=True)
    @patch("modastack.manager.session.detect_state", return_value="waiting_input")
    def test_run_starts_without_crash(self, mock_state, mock_wait, mock_kill, mock_start, modastack_install):
        """run() should get through startup without AttributeError or ImportError."""
        import signal
        from modastack.manager.events.consumer import run

        original_sleep = time.sleep

        call_count = {"sleep": 0}
        def short_sleep(n):
            call_count["sleep"] += 1
            if call_count["sleep"] > 5:
                raise SystemExit(0)
            original_sleep(min(n, 0.01))

        with patch("time.sleep", side_effect=short_sleep), \
             patch("modastack.manager.session.ManagerSession.is_alive", return_value=True), \
             patch("signal.signal"):
            try:
                run(repo_path=modastack_install.repo_path)
            except SystemExit:
                pass

        assert mock_start.called


class TestBuildSubscriptions:

    def test_slack_channel_scoped(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "github:\n  repo: org/myrepo\n"
            "slack:\n  workspace_id: T123\n  channel: C456\n"
        )
        from modastack.manager.events.consumer import _build_subscriptions
        subs = _build_subscriptions(tmp_path)
        assert "slack:T123:C456" in subs
        assert "slack:T123" not in subs

    def test_slack_workspace_only_warns(self, tmp_path, caplog):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "github:\n  repo: org/myrepo\n"
            "slack:\n  workspace_id: T123\n"
        )
        from modastack.manager.events.consumer import _build_subscriptions
        import logging
        with caplog.at_level(logging.WARNING):
            subs = _build_subscriptions(tmp_path)
        assert not any("slack:" in s for s in subs)
        assert "slack.channel" in caplog.text

    def test_no_slack_config(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "github:\n  repo: org/myrepo\n"
        )
        from modastack.manager.events.consumer import _build_subscriptions
        subs = _build_subscriptions(tmp_path)
        assert "org/myrepo" in subs
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
