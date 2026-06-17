"""Unit tests for Session._process_message and the asyncio.Event wake mechanism.

Tests verify that _process_message wakes immediately (no polling) when
the session transitions to waiting_input, stopped, or error.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from modastack.inbox import Message
from modastack.session import Session


@pytest.fixture
def session(modastack_install):
    """Create a Session without starting it (no Claude client needed)."""
    s = Session(name="test-wake", cwd=str(modastack_install.repo_path))
    # Simulate what _run does: create the asyncio.Event
    s._input_ready = asyncio.Event()
    # Stub out SDK calls that _process_message uses
    s._client = None
    # Replace inbox.respond with a mock so we can verify calls
    s.inbox.respond = MagicMock()
    return s


def _make_msg(wait=False):
    return Message(id="m1", sender="test", text="hello", wait=wait)


def _fake_client(session, drain_response="response text"):
    """Attach a fake client with query() and a fake _drain_turn."""

    class FakeClient:
        async def query(self, text):
            pass

    async def fake_drain():
        session._set_state("waiting_input")
        return drain_response

    session._client = FakeClient()
    session._drain_turn = fake_drain


class TestInputReadyWake:
    """Verify _process_message wakes on asyncio.Event transitions."""

    @pytest.mark.asyncio
    async def test_wake_on_waiting_input(self, session):
        """Message is processed when session transitions to waiting_input."""
        session._set_state("waiting_input")
        _fake_client(session)

        msg = _make_msg(wait=True)
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "response text")

    @pytest.mark.asyncio
    async def test_wake_on_stopped(self, session):
        """Message is rejected when session transitions to stopped."""
        session._set_state("working")

        msg = _make_msg(wait=True)

        async def transition():
            await asyncio.sleep(0.05)
            session._set_state("stopped")

        asyncio.create_task(transition())
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "session stopped")

    @pytest.mark.asyncio
    async def test_wake_on_error(self, session):
        """Message is rejected when session transitions to error."""
        session._set_state("working")

        msg = _make_msg(wait=True)

        async def transition():
            await asyncio.sleep(0.05)
            session._set_state("error")

        asyncio.create_task(transition())
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "session error")

    @pytest.mark.asyncio
    async def test_no_poll_latency(self, session):
        """Transition to waiting_input wakes waiter instantly, not after 0.5s."""
        session._set_state("working")
        _fake_client(session)

        msg = _make_msg(wait=False)

        async def transition():
            await asyncio.sleep(0.01)
            session._set_state("waiting_input")

        asyncio.create_task(transition())

        start = time.monotonic()
        await session._process_message(msg)
        elapsed = time.monotonic() - start

        # Should complete in well under 0.5s (the old poll interval)
        assert elapsed < 0.3

    @pytest.mark.asyncio
    async def test_nonblocking_msg_on_stopped(self, session):
        """Non-blocking message on stopped session returns without error."""
        session._set_state("stopped")

        msg = _make_msg(wait=False)
        await session._process_message(msg)

        # No respond call expected for non-blocking messages
        session.inbox.respond.assert_not_called()


class TestSetState:
    """Verify _set_state fires the asyncio.Event correctly."""

    def test_fires_event_on_waiting_input(self, session):
        session._input_ready.clear()
        session._set_state("waiting_input")
        assert session._input_ready.is_set()

    def test_fires_event_on_error(self, session):
        session._input_ready.clear()
        session._set_state("error")
        assert session._input_ready.is_set()

    def test_fires_event_on_stopped(self, session):
        session._input_ready.clear()
        session._set_state("stopped")
        assert session._input_ready.is_set()

    def test_does_not_fire_on_working(self, session):
        session._input_ready.clear()
        session._set_state("working")
        assert not session._input_ready.is_set()

    def test_does_not_fire_on_running(self, session):
        session._input_ready.clear()
        session._set_state("running")
        assert not session._input_ready.is_set()
