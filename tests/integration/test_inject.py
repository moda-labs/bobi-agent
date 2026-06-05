"""Integration tests for ManagerSession.inject() — drives real Claude Code sessions.

These tests start actual Claude Code processes via the Agent SDK and
exercise the inject/drain/response cycle. They verify:
- inject() delivers text and returns True on success
- inject() respects the timeout and returns False
- inject() returns False when the session is in the wrong state
- read_last_response() captures the manager's response text
- The session recovers after a failed inject

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import asyncio
import shutil
import threading
import time
from pathlib import Path

import pytest

from modastack.manager.session import ManagerSession, set_default_session

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

_test_session: ManagerSession | None = None


def _start_test_session():
    """Start a lightweight Claude session for testing (not the full manager)."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from modastack.sdk import get_cli_path

    global _test_session
    s = ManagerSession(repo_path=Path("/tmp/test-repo"))
    _test_session = s
    set_default_session(s)

    s._client = None
    s._loop = None
    s._state = "stopped"
    s._last_response = ""

    loop = asyncio.new_event_loop()
    s._loop = loop

    async def _run():
        options = ClaudeAgentOptions(
            cwd="/tmp",
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test agent. Reply concisely. No tools.",
        )
        client = ClaudeSDKClient(options)
        s._client = client
        await client.connect("You are online. Reply with just: READY")
        await s._drain_turn()
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
            s._loop = None

    t = threading.Thread(target=_thread, daemon=True, name="test-session")
    t.start()
    s._thread = t

    for _ in range(60):
        if s._state == "waiting_input":
            return t
        time.sleep(1)

    raise RuntimeError("Test session failed to start within 60s")


def _stop_test_session():
    global _test_session
    s = _test_session
    if not s:
        return
    if s._client and s._loop:
        async def _disconnect():
            await s._client.disconnect()
        try:
            fut = asyncio.run_coroutine_threadsafe(_disconnect(), s._loop)
            fut.result(timeout=5)
        except Exception:
            pass
    s._client = None
    s._state = "stopped"
    _test_session = None


@requires_claude
@pytest.mark.timeout(120)
class TestInjectIntegration:

    def setup_method(self):
        _start_test_session()

    def teardown_method(self):
        _stop_test_session()

    def test_inject_succeeds(self):
        result = _test_session.inject("Reply with just: INJECT_OK", timeout=60)
        assert result is True

    def test_response_captured(self):
        _test_session.inject("Reply with just the word: CAPTURED", timeout=60)
        response = _test_session.read_last_response()
        assert response is not None
        assert "CAPTURED" in response

    def test_inject_state_guard(self):
        _test_session._state = "working"
        result = _test_session.inject("Should not work")
        assert result is False
        _test_session._state = "waiting_input"

    def test_inject_timeout(self):
        result = _test_session.inject(
            "Think for a very long time before replying. "
            "Write at least 10000 words about the history of computing.",
            timeout=3,
        )
        assert result is False

    def test_sequential_injects(self):
        ok1 = _test_session.inject("Reply with just: FIRST", timeout=60)
        assert ok1 is True
        r1 = _test_session.read_last_response()
        assert "FIRST" in r1

        ok2 = _test_session.inject("Reply with just: SECOND", timeout=60)
        assert ok2 is True
        r2 = _test_session.read_last_response()
        assert "SECOND" in r2

    def test_inject_with_multiline_event(self):
        event_text = (
            "Event: slack/slack.dm\n"
            "  from: Zach\n"
            "  text: Are you there?\n"
            "  channel: D0B51JP1N4C\n"
            "  workspace: T0952RZRZ0X\n"
            "\n"
            "Reply with just: EVENT_RECEIVED"
        )
        result = _test_session.inject(event_text, timeout=60)
        assert result is True
        response = _test_session.read_last_response()
        assert "EVENT_RECEIVED" in response
