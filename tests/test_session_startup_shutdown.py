"""Regression tests for startup/shutdown honesty (D003, D021).

Phase 1 of the review-remediation plan: a session's own lifecycle signals must
not lie about whether it is still running. Two ways they did:

* ``stop()`` was a no-op while the startup turn was still in flight, because
  ``_keep_alive`` is only created *after* the startup drain. ``stop()`` guarded
  on ``if self._keep_alive:``, so the shutdown signal was dropped and the
  session thread plus its brain subprocess outlived stop() forever - a leaked
  agent per timed-out phase, burning tokens inside a long-lived orchestrator
  process while the phase was reported as "session failed to start" (D003).
* ``start()`` waited the full timeout on ``_ready`` even after the session
  thread had already crashed, because it never checked thread liveness. A
  launch that fails instantly stalled dispatch for up to the whole timeout -
  3600s on the spawn paths - instead of failing fast (D021).
"""

import asyncio
import threading
import time

from bobi.brain import TurnResult
from bobi.session import Session


def _result():
    return TurnResult(
        session_id="sess-1",
        is_error=False,
        api_error_status=None,
        total_cost_usd=0.0,
        duration_ms=1,
        num_turns=1,
        result_text="ok",
    )


class _HangingClient:
    """A brain whose startup turn never completes on its own.

    The stall is an ``await`` (not a synchronous sleep) so the event loop keeps
    pumping - a cancellation can actually land. This is the wedged-startup-turn
    double: the turn is in flight, so ``_keep_alive`` does not exist yet.
    """

    provider = "anthropic"

    def __init__(self):
        self.disconnected = False
        self.turn_started = threading.Event()

    async def connect(self, prompt=None):
        pass

    async def query(self, text):  # pragma: no cover - startup prompt rides connect
        pass

    async def receive_response(self):
        self.turn_started.set()
        await asyncio.sleep(3600)
        yield _result()  # pragma: no cover - the turn never gets here

    async def disconnect(self):
        self.disconnected = True


class _DeadOnConnectClient:
    """A brain that cannot connect - the missing-CLI / instant-crash double."""

    provider = "anthropic"

    def __init__(self):
        self.disconnected = False

    async def connect(self, prompt=None):
        raise RuntimeError("brain CLI missing")

    async def query(self, text):  # pragma: no cover - never reached
        pass

    async def receive_response(self):  # pragma: no cover - never reached
        yield _result()

    async def disconnect(self):
        self.disconnected = True


def _session(name, cwd, client):
    """A Session wired to *client*, with no event-server subscription."""

    class _TestSession(Session):
        def _make_brain_session(self, resume=None):
            return client

        def _start_subscription(self):
            pass  # no event server in this test

    return _TestSession(name=name, cwd=cwd)


def test_stop_tears_down_a_session_still_in_its_startup_turn(bobi_install, monkeypatch):
    """stop() must interrupt a startup turn that never finishes (D003).

    Before the fix ``_keep_alive`` was still None at this point, so stop()
    silently dropped the shutdown signal: the join expired, the thread stayed
    alive holding a connected brain, and nothing ever disconnected it.
    """
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    client = _HangingClient()
    session = _session("stop-during-startup", str(bobi_install.repo_path), client)

    # The startup turn hangs, so start() gives up on its own short timeout.
    assert session.start("go", timeout=2) is False
    assert client.turn_started.wait(timeout=5), "startup turn never began"

    session.stop()

    assert not session._thread.is_alive(), (
        "session thread outlived stop() - the brain subprocess leaks"
    )
    assert client.disconnected, "brain session was never disconnected"
    assert session.detect_state() == "stopped"


def test_start_returns_promptly_when_the_session_thread_dies(bobi_install, monkeypatch):
    """start() must fail fast once the thread is gone, not wait out the timeout (D021).

    connect() raises with no resume id, so _run propagates, _thread_target
    records 'error' and the thread exits within milliseconds - but _ready is
    never set, so the old start() sat on it for the entire timeout.
    """
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    client = _DeadOnConnectClient()
    session = _session("dead-on-connect", str(bobi_install.repo_path), client)

    began = time.monotonic()
    assert session.start("go", timeout=20) is False
    elapsed = time.monotonic() - began

    assert elapsed < 5, (
        f"start() waited {elapsed:.1f}s on a thread that died immediately"
    )
    assert session.detect_state() == "error"


def test_a_stopped_session_can_be_started_again(bobi_install, monkeypatch):
    """The shutdown latches must not persist across a restart (D003 guard).

    ``_stop_requested`` and ``_keep_alive`` survive on the object after stop().
    If start() did not reset them, the next run would honour the *previous*
    stop the instant it became ready and tear itself straight back down.
    """
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)

    class _ReadyClient(_HangingClient):
        async def receive_response(self):
            self.turn_started.set()
            yield _result()

    session = _session("restartable", str(bobi_install.repo_path), _ReadyClient())
    assert session.start("go", timeout=20) is True
    session.stop()
    assert not session._thread.is_alive()

    # Second life: must come up and stay up.
    session._client = None
    assert session.start("go", timeout=20) is True
    try:
        assert session._thread.is_alive()
        assert session.detect_state() == "waiting_input"
    finally:
        session.stop()
    assert not session._thread.is_alive()


def test_start_still_reports_ready_on_the_happy_path(bobi_install, monkeypatch):
    """The liveness watch must not break a normal start (D021 guard).

    A thread that sets _ready and keeps running has to keep returning True -
    the early-exit must trigger on death, not merely on "not ready yet".
    """
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)

    class _ReadyClient(_HangingClient):
        async def receive_response(self):
            self.turn_started.set()
            yield _result()

    client = _ReadyClient()
    session = _session("healthy-start", str(bobi_install.repo_path), client)
    try:
        assert session.start("go", timeout=20) is True
        assert session.detect_state() == "waiting_input"
    finally:
        session.stop()

    assert not session._thread.is_alive()
    assert client.disconnected
