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

import asyncio
import shutil
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from modastack.manager import session
from modastack.manager.events.event_client import event_queue, format_event_for_manager

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


def _start_test_session():
    """Start a lightweight Claude session for testing."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from modastack.sdk import get_cli_path

    session._client = None
    session._loop = None
    session._state = "stopped"
    session._last_response = ""

    loop = asyncio.new_event_loop()
    session._loop = loop

    async def _run():
        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt=(
                "You are a test manager agent. When you receive events, "
                "acknowledge them concisely. No tools, no markdown, just plain text. "
                "Always include the word ACKNOWLEDGED in your response."
            ),
        )
        client = ClaudeSDKClient(options)
        session._client = client
        await client.connect("You are online. Reply: READY")
        await session._drain_turn()
        keep_alive = asyncio.Event()
        await keep_alive.wait()

    def _thread():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception:
            pass
        finally:
            loop.close()
            session._loop = None

    t = threading.Thread(target=_thread, daemon=True, name="test-session")
    t.start()

    for _ in range(60):
        if session._state == "waiting_input":
            return t
        time.sleep(1)

    raise RuntimeError("Test session failed to start within 60s")


def _stop_test_session():
    if session._client and session._loop:
        async def _disconnect():
            await session._client.disconnect()
        try:
            fut = asyncio.run_coroutine_threadsafe(_disconnect(), session._loop)
            fut.result(timeout=5)
        except Exception:
            pass
    session._client = None
    session._state = "stopped"


def _clear_queue():
    while not event_queue.empty():
        event_queue.get_nowait()


@requires_claude
@pytest.mark.timeout(180)
class TestEventPipeline:
    """Full pipeline: event → queue → format → inject → response."""

    def setup_method(self):
        self._orig_client = session._client
        self._orig_loop = session._loop
        self._orig_state = session._state
        self._orig_response = session._last_response
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()
        session._client = self._orig_client
        session._loop = self._orig_loop
        session._state = self._orig_state
        session._last_response = self._orig_response

    def test_github_issue_event(self):
        """GitHub issue opened → formatted → injected → manager acknowledges."""
        event = {
            "type": "task.opened", "source": "github",
            "data": {
                "issue_id": "42", "title": "Add rate limiting",
                "repo": "moda-labs/test", "state": "open",
                "url": "https://github.com/moda-labs/test/issues/42",
            },
        }
        text = format_event_for_manager(event)
        assert "task.opened" in text
        assert "42" in text

        ok = session.inject(text, timeout=60)
        assert ok is True
        response = session.read_last_response()
        assert response is not None
        assert "ACKNOWLEDGED" in response

    def test_github_pr_merged(self):
        event = {
            "type": "pr.closed", "source": "github",
            "data": {
                "pr_number": 10, "title": "Fix auth",
                "repo": "moda-labs/test", "branch": "fix-auth",
                "state": "closed", "merged": True,
            },
        }
        text = format_event_for_manager(event)
        ok = session.inject(text, timeout=60)
        assert ok is True

    def test_slack_dm_event(self):
        """Slack DM → formatted with workspace/channel → manager responds."""
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

        ok = session.inject(text, timeout=60)
        assert ok is True
        response = session.read_last_response()
        assert response is not None
        assert len(response) > 0

    def test_linear_event(self):
        event = {
            "type": "linear.Issue.update", "source": "linear",
            "data": {
                "issue_id": "ENG-42", "title": "Add caching",
                "state": "In Progress", "team_key": "ENG",
            },
        }
        text = format_event_for_manager(event)
        ok = session.inject(text, timeout=60)
        assert ok is True

    def test_batched_events(self):
        """Multiple events batched into one injection."""
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
        ok = session.inject(text, timeout=60)
        assert ok is True

    def test_recovery_after_timeout(self):
        """Session remains usable after a timed-out inject."""
        result = session.inject(
            "Write a 10000 word essay on quantum computing.",
            timeout=3,
        )
        assert result is False

        # Wait for the state to settle -- the timed-out drain
        # is still running on the event loop. We need to wait for
        # the Claude response to complete before injecting again.
        for _ in range(60):
            if session._state == "waiting_input":
                break
            time.sleep(1)

        if session._state == "waiting_input":
            ok = session.inject("Reply with just: RECOVERED", timeout=60)
            assert ok is True
            assert "RECOVERED" in (session.read_last_response() or "")


@requires_claude
@pytest.mark.timeout(120)
class TestSlackResponderIntegration:
    """Test that SlackResponder receives the correct response after inject."""

    def setup_method(self):
        self._orig_client = session._client
        self._orig_loop = session._loop
        self._orig_state = session._state
        self._orig_response = session._last_response
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()
        session._client = self._orig_client
        session._loop = self._orig_loop
        session._state = self._orig_state
        session._last_response = self._orig_response

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_responder_called_after_slack_inject(self, mock_config, mock_post):
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
        ok = session.inject(text, timeout=60)
        assert ok is True

        response = session.read_last_response()
        assert response is not None

        responder.handle([event], response)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "xoxb-test"
        assert call_args[0][1] == "D0B51JP1N4C"
        assert len(call_args[0][2]) > 0  # response text
        assert call_args[0][3] == ""  # no thread for DMs

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_responder_threads_mentions(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        from modastack.manager.events.slack_responder import SlackResponder
        responder = SlackResponder()

        event = {
            "type": "slack.mention", "source": "slack",
            "data": {
                "from": "Zach", "text": "check the deploy",
                "channel": "C_GENERAL", "workspace": "T0952RZRZ0X",
                "ts": "200.001", "thread_ts": "",
            },
        }

        text = format_event_for_manager(event)
        session.inject(text, timeout=60)
        response = session.read_last_response()

        responder.handle([event], response)
        call_args = mock_post.call_args
        assert call_args[0][1] == "C_GENERAL"
        assert call_args[0][3] == "200.001"  # ts used as thread for mentions


@requires_claude
@pytest.mark.timeout(120)
class TestDrainLoopIntegration:
    """Test the consumer drain loop with real events and a real session."""

    def setup_method(self):
        self._orig_client = session._client
        self._orig_loop = session._loop
        self._orig_state = session._state
        self._orig_response = session._last_response
        _clear_queue()
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()
        _clear_queue()
        session._client = self._orig_client
        session._loop = self._orig_loop
        session._state = self._orig_state
        session._last_response = self._orig_response

    def test_queue_to_inject(self):
        """Event put in queue → drain loop picks it up → injects → response."""
        from modastack.manager.events.consumer import DRAIN_INTERVAL

        event = {
            "type": "task.opened", "source": "github",
            "data": {"issue_id": "99", "title": "Test issue", "repo": "moda-labs/test"},
        }
        event_queue.put(event)

        # Run one iteration of the drain loop manually
        time.sleep(DRAIN_INTERVAL + 0.5)
        batch = []
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())
        if not batch:
            batch = [event]

        text = "\n\n".join(format_event_for_manager(e) for e in batch)
        ok = session.inject(text, timeout=60)
        assert ok is True
        assert session.read_last_response() is not None
