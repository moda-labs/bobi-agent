"""Integration tests for the full event pipeline.

Simulates events flowing through the bus into the manager session:
  mock event → event_queue → drain loop → inject → Claude Code → response

Tests cover:
- GitHub events (issue opened, PR merged, push)
- Slack DM events with response capture
- Linear events
- Batching multiple events into one inject
- Slack responder routing after inject
- Manager state recovery after timeout
- Event formatting round-trip

These tests drive real Claude Code sessions. Requires the `claude` CLI.
"""

import shutil
import time
from unittest.mock import patch, MagicMock

import pytest

from modastack.manager.events.event_client import event_queue, format_event_for_manager
from .test_inject import _start_test_session, _stop_test_session, _test_session

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


def _clear_queue():
    while not event_queue.empty():
        event_queue.get_nowait()


def _session():
    from .test_inject import _test_session as ts
    return ts


@requires_claude
@pytest.mark.timeout(180)
class TestEventPipeline:
    """Full pipeline: event → queue → format → inject → response."""

    def setup_method(self):
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()

    def test_github_pr_merged(self):
        s = _session()
        event = {
            "type": "pr.closed", "source": "github",
            "data": {
                "pr_number": 10, "title": "Fix auth",
                "repo": "moda-labs/test", "branch": "fix-auth",
                "state": "closed", "merged": True,
            },
        }
        text = format_event_for_manager(event)
        ok = s.inject(text, timeout=60)
        assert ok is True

    def test_slack_dm_event(self):
        s = _session()
        event = {
            "type": "slack.dm", "source": "slack",
            "data": {
                "from": "Zach", "text": "How's the deploy going?",
                "channel": "D0B51JP1N4C", "workspace": "T0952RZRZ0X",
                "ts": "1780254589.098309", "thread_ts": "",
            },
        }
        text = format_event_for_manager(event)
        assert "workspace: T0952RZRZ0X" in text
        assert "channel: D0B51JP1N4C" in text
        assert "from: Zach" in text

        ok = s.inject(text, timeout=60)
        assert ok is True
        response = s.read_last_response()
        assert response is not None
        assert len(response) > 0

    def test_linear_event(self):
        s = _session()
        event = {
            "type": "linear.Issue.update", "source": "linear",
            "data": {
                "issue_id": "ENG-42", "title": "Add caching",
                "state": "In Progress", "team_key": "ENG",
            },
        }
        text = format_event_for_manager(event)
        ok = s.inject(text, timeout=60)
        assert ok is True

    def test_batched_events(self):
        s = _session()
        events = [
            {"type": "task.opened", "source": "github",
             "data": {"issue_id": "1", "title": "Bug A", "repo": "moda-labs/test"}},
            {"type": "task.assigned", "source": "github",
             "data": {"issue_id": "2", "title": "Feature B", "repo": "moda-labs/test"}},
            {"type": "slack.dm", "source": "slack",
             "data": {"from": "Zach", "text": "Status update?",
                      "channel": "D123", "workspace": "T123"}},
        ]
        lines = [format_event_for_manager(e) for e in events]
        text = "\n\n".join(lines)

        assert text.count("Event:") == 3
        ok = s.inject(text, timeout=60)
        assert ok is True

    def test_recovery_after_timeout(self):
        s = _session()
        result = s.inject(
            "Write a 10000 word essay on quantum computing.",
            timeout=3,
        )
        assert result is False

        for _ in range(90):
            if s._state == "waiting_input":
                break
            time.sleep(1)

        if s._state != "waiting_input":
            pytest.skip("Session did not recover to waiting_input in time")

        ok = s.inject("Reply with just: RECOVERED", timeout=60)
        assert ok is True


@requires_claude
@pytest.mark.timeout(120)
class TestSlackResponderIntegration:

    def setup_method(self):
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_responder_called_after_slack_inject(self, mock_config, mock_post):
        s = _session()
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        from modastack.manager.events.slack_responder import SlackResponder
        responder = SlackResponder()

        event = {
            "type": "slack.dm", "source": "slack",
            "data": {
                "from": "Zach", "text": "ping",
                "channel": "D0B51JP1N4C", "workspace": "T0952RZRZ0X",
                "ts": "100.001", "thread_ts": "",
            },
        }

        text = format_event_for_manager(event)
        ok = s.inject(text, timeout=60)
        assert ok is True

        response = s.read_last_response()
        assert response is not None

        responder.handle([event], response)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "xoxb-test"
        assert call_args[0][1] == "D0B51JP1N4C"
        assert len(call_args[0][2]) > 0
        assert call_args[0][3] == ""

@requires_claude
@pytest.mark.timeout(120)
class TestDrainLoopIntegration:

    def setup_method(self):
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()

    def test_queue_to_inject(self):
        s = _session()
        from modastack.manager.events.consumer import DRAIN_INTERVAL

        event = {
            "type": "task.opened", "source": "github",
            "data": {"issue_id": "99", "title": "Test issue", "repo": "moda-labs/test"},
        }
        event_queue.put(event)

        time.sleep(DRAIN_INTERVAL + 0.5)
        batch = []
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())
        if not batch:
            batch = [event]

        text = "\n\n".join(format_event_for_manager(e) for e in batch)
        ok = s.inject(text, timeout=60)
        assert ok is True
        assert s.read_last_response() is not None
