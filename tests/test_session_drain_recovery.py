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

from bobi.brain import TurnResult
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
