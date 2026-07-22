"""Drain-failure classification tests (#719 / #718).

A drain failure used to be terminal: the session dropped to status="error" on
the FIRST exception and stayed dead until a process restart. A single oversized
NDJSON message (the 1 MB buffer death) therefore left a director dead for tens
of minutes (#718).

The drain now distinguishes a recoverable per-message decode error from a dead
transport:

- a per-message decode error flags a rotation (the bounded, well-tested
  idle-time ``_rotate()`` rebuilds the client) and returns the session to
  ``waiting_input`` — the session survives, the drain "continues" on the next
  turn,
- a genuinely dead transport still transitions to the terminal ``error`` state
  (a fresh process start recovers it — the supervisor owns that).
"""

import asyncio

import pytest

from bobi.brain import TurnResult
from bobi.inbox import Message
from bobi.session import Session, _is_decode_error


class _FakeClient:
    """A brain-session double for _drain_turn. ``drain_error`` (if set) is
    raised from receive_response() to simulate a turn that blows up mid-drain."""

    def __init__(self, *, drain_error=None):
        self._drain_error = drain_error
        self.provider = "anthropic"

    async def query(self, text):
        pass

    async def receive_response(self):
        if self._drain_error is not None:
            err, self._drain_error = self._drain_error, None
            raise err
        yield TurnResult(session_id="sess", is_error=False)


def _session(bobi_install, client):
    s = Session(name="drain-recover", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = client
    return s


# --- classifier ------------------------------------------------------------

def test_buffer_overflow_is_a_decode_error():
    exc = Exception(
        "Failed to decode JSON: JSON message exceeded maximum buffer size of "
        "1048576 bytes"
    )
    assert _is_decode_error(exc) is True


def test_sdk_decode_error_type_is_a_decode_error():
    from claude_agent_sdk import CLIJSONDecodeError

    assert _is_decode_error(CLIJSONDecodeError("bad line", ValueError("x"))) is True


def test_plain_transport_failure_is_not_a_decode_error():
    assert _is_decode_error(RuntimeError("transport closed unexpectedly")) is False


# --- classification-driven recovery behaviour ------------------------------

def test_decode_error_flags_rotation_and_stays_ready(bobi_install):
    """A single oversized message must not kill the session: it flags a rotation
    (idle _rotate() rebuilds the client) and returns to ready — the drain
    'continues' on the next turn instead of dying."""
    buffer_err = Exception(
        "JSON message exceeded maximum buffer size of 1048576 bytes"
    )
    s = _session(bobi_install, _FakeClient(drain_error=buffer_err))

    asyncio.run(s._drain_turn())

    assert s._state == "waiting_input"       # not killed
    assert s._rotate_pending is True         # idle loop will rebuild the client
    assert s._rotate_reason == "drain_decode_error"


def test_decode_error_clears_stale_turn_error_state(bobi_install):
    """Returning to waiting_input after a decode error must not let a prior
    turn's transient-error flag drive a spurious self-heal retry."""
    s = _session(bobi_install, _FakeClient(
        drain_error=Exception("failed to decode json")
    ))
    s._last_is_error = True  # left over from an earlier transient (e.g. 529) turn

    asyncio.run(s._drain_turn())

    assert s._last_is_error is False


def test_dead_transport_transitions_to_error(bobi_install):
    """Only a genuinely dead transport (a non-decode drain failure) transitions
    to the terminal error state; it does not flag a rotation."""
    s = _session(bobi_install, _FakeClient(
        drain_error=RuntimeError("transport gone: broken pipe")
    ))

    asyncio.run(s._drain_turn())

    assert s._state == "error"
    assert s._rotate_pending is False


@pytest.mark.asyncio
async def test_decode_error_replaces_spent_client_before_next_message(
    bobi_install,
):
    """Queued work must wait for replacement of a spent response reader."""

    class SpentClient:
        provider = "anthropic"

        def __init__(self):
            self.queries = []

        async def query(self, text):
            self.queries.append(text)

        async def receive_response(self):
            raise RuntimeError("failed to decode json")
            yield  # pragma: no cover - required for an async generator

        async def disconnect(self):
            pass

    class FreshClient:
        provider = "anthropic"

        def __init__(self):
            self.queries = []

        async def connect(self, prompt=None):
            pass

        async def query(self, text):
            self.queries.append(text)

        async def receive_response(self):
            yield TurnResult(session_id="fresh", is_error=False)

        async def disconnect(self):
            pass

    spent = SpentClient()
    fresh = FreshClient()
    s = _session(bobi_install, spent)
    s._set_state("waiting_input")
    s._make_brain_session = lambda resume=None: fresh
    recv = s.inbox.recv
    s.inbox.recv = lambda timeout=2.0: recv(timeout=0.01)
    s.inbox.start()
    s._keep_alive = asyncio.Event()

    acknowledgements = []
    first = Message(id="first", sender="event-bus", text="first")
    first.on_done = lambda: acknowledgements.append("first")
    second = Message(id="second", sender="event-bus", text="second")
    second.on_done = lambda: acknowledgements.append("second")
    s.inbox.push(first)
    s.inbox.push(second)

    inbox_task = asyncio.create_task(s._inbox_loop())
    try:
        async def second_was_processed():
            while "second" not in acknowledgements:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(second_was_processed(), timeout=1.0)
    finally:
        s._keep_alive.set()
        await asyncio.wait_for(inbox_task, timeout=1.0)
        await s._stop_rotation_work()
        s.inbox.close()

    assert spent.queries == ["first"]
    assert fresh.queries == ["second"]
    assert acknowledgements == ["second"]


@pytest.mark.asyncio
async def test_terminal_decode_recovery_stops_before_dequeuing_more_messages(
    bobi_install, monkeypatch
):
    """A failed replacement leaves queued events intact for restart replay."""
    monkeypatch.setattr("bobi.session.ROTATION_MAX_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr("bobi.session.ROTATION_RECONNECT_BACKOFF", 0.0)

    class SpentClient:
        provider = "anthropic"

        def __init__(self):
            self.queries = []

        async def query(self, text):
            self.queries.append(text)

        async def receive_response(self):
            raise RuntimeError("failed to decode json")
            yield  # pragma: no cover - required for an async generator

        async def disconnect(self):
            pass

    class FailedReplacement:
        async def connect(self, prompt=None):
            raise RuntimeError("replacement unavailable")

        async def disconnect(self):
            pass

    spent = SpentClient()
    s = _session(bobi_install, spent)
    s._set_state("waiting_input")
    s._make_brain_session = lambda resume=None: FailedReplacement()
    recv = s.inbox.recv
    s.inbox.recv = lambda timeout=2.0: recv(timeout=0.01)
    s.inbox.start()
    s._keep_alive = asyncio.Event()
    s.inbox.push(Message(id="first", sender="event-bus", text="first"))
    s.inbox.push(Message(id="second", sender="event-bus", text="second"))

    inbox_task = asyncio.create_task(s._inbox_loop())
    try:
        async def recovery_failed():
            while s.detect_state() != "error":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(recovery_failed(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert not s.inbox.empty(), "terminal recovery consumed queued replay work"
    finally:
        s._keep_alive.set()
        await asyncio.wait_for(inbox_task, timeout=1.0)
        await s._stop_rotation_work()
        s.inbox.close()

    assert spent.queries == ["first"]


@pytest.mark.asyncio
async def test_context_rotation_starts_during_sustained_inbox_traffic(
    bobi_install,
):
    """An always-busy inbox must not starve an already-pending rotation."""

    class ActiveClient:
        provider = "anthropic"

        async def query(self, text):
            pass

        async def receive_response(self):
            yield TurnResult(session_id="active", is_error=False)

        async def disconnect(self):
            pass

    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    class CandidateClient:
        provider = "anthropic"

        async def connect(self, prompt=None):
            connect_started.set()
            await release_connect.wait()

        async def disconnect(self):
            release_connect.set()

    session = _session(bobi_install, ActiveClient())
    session._set_state("waiting_input")
    session._rotate_pending = True
    session._rotate_reason = "context_cap"
    session._make_brain_session = lambda resume=None: CandidateClient()
    session.inbox.recv = lambda timeout=2.0: Message(
        id="busy", sender="event-bus", text="continuous work"
    )
    session._keep_alive = asyncio.Event()

    inbox_task = asyncio.create_task(session._inbox_loop())
    try:
        await asyncio.wait_for(connect_started.wait(), timeout=0.1)
    finally:
        release_connect.set()
        inbox_task.cancel()
        await asyncio.gather(inbox_task, return_exceptions=True)
        await session._stop_rotation_work()


@pytest.mark.asyncio
async def test_rotation_commit_failure_surfaces_terminally(
    bobi_install, monkeypatch
):
    """Persistence failure during a candidate swap must not kill the loop silently."""

    class Client:
        provider = "anthropic"

        def __init__(self):
            self.is_disconnected = False

        async def disconnect(self):
            self.is_disconnected = True

    active = Client()
    candidate = Client()
    session = _session(bobi_install, active)
    session._set_state("waiting_input")
    session._rotate_pending = True

    async def prepared():
        return candidate, "context_cap"

    session._rotation_task = asyncio.create_task(prepared())
    await asyncio.sleep(0)

    def fail_session_save(*args, **kwargs):
        raise OSError("cursor filesystem unavailable")

    monkeypatch.setattr(
        "bobi.session.save_session_id",
        fail_session_save,
    )

    await session._commit_ready_rotation()

    assert session.detect_state() == "error"
    assert session._rotation_task is None
    assert session._client is active
    assert candidate.is_disconnected is True
