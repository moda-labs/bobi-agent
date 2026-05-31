"""Integration tests for the full event lifecycle.

End-to-end: event arrives → manager decides → action taken → Slack reply sent.

These tests use the real manager prompt so the manager behaves like production.
External side effects (spawning engineers, posting to Slack) are intercepted
via subprocess and HTTP mocks so the tests are self-contained.

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from modastack.manager import session
from modastack.manager.events.event_client import event_queue, format_event_for_manager
from modastack.manager.events.slack_responder import SlackResponder

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

MANAGER_PROMPT = """\
You are a test manager. You receive events and take action.

When you receive a task.assigned event, run this command:
```bash
echo "SPAWN:$REPO:$ISSUE"
```
Replace $REPO with the repo name and $ISSUE with the issue ID from the event.

When you receive a slack.dm event, reply to the human using:
```bash
modastack slack-reply -w $WORKSPACE -c $CHANNEL "$YOUR_RESPONSE"
```
Replace the variables from the event. Keep your response under 50 words.

When you receive a task.closed or pr.closed event, just say NOTED.

Always take action — never just describe what you would do.
"""


def _start_manager_session(prompt: str = MANAGER_PROMPT):
    """Start a Claude session with the given prompt."""
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
            cwd=os.getcwd(),
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt=prompt,
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

    t = threading.Thread(target=_thread, daemon=True, name="test-manager")
    t.start()

    for _ in range(60):
        if session._state == "waiting_input":
            return t
        time.sleep(1)

    raise RuntimeError("Test session failed to start within 60s")


def _stop_session():
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
class TestIssueAssignedTriggersSpawn:
    """Issue assigned → manager runs spawn command."""

    def setup_method(self):
        self._orig = (session._client, session._loop, session._state, session._last_response)
        _clear_queue()
        _start_manager_session()

    def teardown_method(self):
        _stop_session()
        _clear_queue()
        session._client, session._loop, session._state, session._last_response = self._orig

    def test_manager_spawns_on_issue_assigned(self):
        event = {
            "type": "task.assigned", "source": "github",
            "data": {
                "issue_id": "42", "title": "Add rate limiting",
                "repo": "moda-labs/bettertab", "state": "open",
                "url": "https://github.com/moda-labs/bettertab/issues/42",
            },
        }
        text = format_event_for_manager(event)
        ok = session.inject(text, timeout=90)
        assert ok is True

        response = session.read_last_response() or ""
        # The manager should have tried to run echo SPAWN:...
        # or at minimum acknowledged with the issue details.
        # Since it has bypassPermissions, it may actually run the command.
        assert "42" in response or "bettertab" in response or "SPAWN" in response


@requires_claude
@pytest.mark.timeout(180)
class TestSlackDMFullCycle:
    """Slack DM → manager responds → SlackResponder delivers."""

    def setup_method(self):
        self._orig = (session._client, session._loop, session._state, session._last_response)
        _clear_queue()
        _start_manager_session()

    def teardown_method(self):
        _stop_session()
        _clear_queue()
        session._client, session._loop, session._state, session._last_response = self._orig

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_dm_gets_reply_delivered(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        event = {
            "type": "slack.dm", "source": "slack",
            "data": {
                "from": "Zach", "text": "What's the status of the deploy?",
                "channel": "D0B51JP1N4C", "workspace": "T0952RZRZ0X",
                "ts": "1780254589.098309", "thread_ts": "",
            },
        }

        # Step 1: Inject the Slack event
        text = format_event_for_manager(event)
        ok = session.inject(text, timeout=90)
        assert ok is True

        # Step 2: Capture the manager's response
        response = session.read_last_response()
        assert response is not None
        assert len(response) > 0

        # Step 3: SlackResponder delivers the reply
        responder = SlackResponder()
        responder.handle([event], response)

        # Step 4: Verify Slack API was called correctly
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert args[0] == "xoxb-test"       # token
        assert args[1] == "D0B51JP1N4C"     # channel
        assert len(args[2]) > 0             # response text
        assert args[3] == ""                # no thread for DMs


@requires_claude
@pytest.mark.timeout(180)
class TestMixedEventBatch:
    """Batch of GitHub + Slack events → manager handles all → Slack gets reply."""

    def setup_method(self):
        self._orig = (session._client, session._loop, session._state, session._last_response)
        _clear_queue()
        _start_manager_session()

    def teardown_method(self):
        _stop_session()
        _clear_queue()
        session._client, session._loop, session._state, session._last_response = self._orig

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_batch_with_slack_gets_reply(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        github_event = {
            "type": "pr.closed", "source": "github",
            "data": {
                "pr_number": 10, "title": "Fix auth",
                "repo": "moda-labs/test", "state": "closed", "merged": True,
            },
        }
        slack_event = {
            "type": "slack.dm", "source": "slack",
            "data": {
                "from": "Zach", "text": "Hey, did that PR land?",
                "channel": "D0B51JP1N4C", "workspace": "T0952RZRZ0X",
                "ts": "100.001", "thread_ts": "",
            },
        }

        batch = [github_event, slack_event]
        lines = [format_event_for_manager(e) for e in batch]
        text = "\n\n".join(lines)

        ok = session.inject(text, timeout=90)
        assert ok is True

        response = session.read_last_response()
        assert response is not None

        # SlackResponder should only post for the Slack event, not GitHub
        responder = SlackResponder()
        responder.handle(batch, response)

        mock_post.assert_called_once()
        assert mock_post.call_args[0][1] == "D0B51JP1N4C"


@requires_claude
@pytest.mark.timeout(180)
class TestChannelMentionThreading:
    """Channel mention → manager responds → reply goes in thread."""

    def setup_method(self):
        self._orig = (session._client, session._loop, session._state, session._last_response)
        _clear_queue()
        _start_manager_session()

    def teardown_method(self):
        _stop_session()
        _clear_queue()
        session._client, session._loop, session._state, session._last_response = self._orig

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_mention_reply_threads(self, mock_config, mock_post):
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        event = {
            "type": "slack.mention", "source": "slack",
            "data": {
                "from": "Zach", "text": "check the deploy status",
                "channel": "C_ENGINEERING", "workspace": "T0952RZRZ0X",
                "ts": "999.001", "thread_ts": "",
            },
        }

        text = format_event_for_manager(event)
        ok = session.inject(text, timeout=90)
        assert ok is True

        response = session.read_last_response()
        responder = SlackResponder()
        responder.handle([event], response)

        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert args[1] == "C_ENGINEERING"   # channel
        assert args[3] == "999.001"         # threaded on the mention's ts


@requires_claude
@pytest.mark.timeout(180)
class TestConsumerDrainToSlackReply:
    """Full drain loop cycle: queue → batch → inject → responder."""

    def setup_method(self):
        self._orig = (session._client, session._loop, session._state, session._last_response)
        _clear_queue()
        _start_manager_session()

    def teardown_method(self):
        _stop_session()
        _clear_queue()
        session._client, session._loop, session._state, session._last_response = self._orig

    @patch("modastack.manager.events.slack_responder._post_to_slack", return_value=True)
    @patch("modastack.manager.events.slack_responder.GlobalConfig")
    def test_drain_loop_triggers_slack_reply(self, mock_config, mock_post):
        """Simulate what the consumer's drain loop does."""
        mock_config.load.return_value = MagicMock()
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        from modastack.manager.events.consumer import DRAIN_INTERVAL

        slack_event = {
            "type": "slack.dm", "source": "slack",
            "data": {
                "from": "Zach", "text": "ping",
                "channel": "D_TEST", "workspace": "T_TEST",
                "ts": "500.001", "thread_ts": "",
            },
        }

        # Simulate what _drain_loop does:
        # 1. Get event from queue
        batch = [slack_event]

        # 2. Format and inject
        lines = [format_event_for_manager(e) for e in batch]
        text = "\n\n".join(lines)

        assert session.detect_state() == "waiting_input"
        ok = session.inject(text, timeout=90)
        assert ok is True

        # 3. Check for Slack events and route response
        has_slack = any(e.get("source") == "slack" for e in batch)
        assert has_slack is True

        response = session.read_last_response() or ""
        assert len(response) > 0

        responder = SlackResponder()
        responder.handle(batch, response)

        # 4. Verify the reply was posted
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert args[0] == "xoxb-test"
        assert args[1] == "D_TEST"
        assert len(args[2]) > 0
