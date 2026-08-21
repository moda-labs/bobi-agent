"""Regression tests for session start/stop honesty (D003, D021, phase 1).

Both bugs are the same shape: a lifecycle call that reports an outcome the
process does not actually reflect.

* **D003** — ``stop()`` cannot shut down a session whose *startup turn* is still
  in flight. ``_keep_alive`` is created only after the startup drain, so
  ``stop()``'s ``if self._keep_alive:`` guard silently no-ops, ``join(15)``
  expires, and the session thread plus its brain subprocess keep running. A
  workflow phase that timed out is reported as "session failed to start" while
  a live agent keeps burning tokens for the life of the orchestrator process.
* **D021** — ``start()`` waits the *full* timeout on ``_ready`` even after the
  session thread has already crashed and exited. With the launch timeouts
  ``run_phase_blocking``/``run_persistent_agent`` actually pass (up to 3600s), a launch
  that fails in milliseconds stalls dispatch for an hour.

Both tests fail on the pre-fix tree: D003's thread stays alive past ``stop()``,
and D021's ``start()`` burns the whole timeout.
"""

import asyncio
import threading
import time

import pytest

from bobi.sdk import SessionEntry, get_registry
from bobi.session import Session


def _register(session, install):
    get_registry().register(
        SessionEntry(
            name=session.name,
            role="director",
            status="starting",
            cwd=str(install.repo_path),
        )
    )


class _HungStartupClient:
    """connect() succeeds, then the startup turn never produces a result.

    Models the D003 scenario exactly: the brain connected, the startup turn is
    in flight, and it outlasts the phase's effective timeout. The hang is an
    ``await``, not a ``time.sleep``, so the event loop keeps pumping — the
    session is genuinely running, not frozen, which is what makes the leak
    expensive rather than merely stuck.
    """

    provider = "anthropic"

    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.turn_started = threading.Event()

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, text):  # pragma: no cover - startup prompt rides connect()
        pass

    async def receive_response(self):
        self.turn_started.set()
        while True:
            await asyncio.sleep(0.01)
        yield  # pragma: no cover - unreachable, keeps this an async generator

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):  # pragma: no cover - not all brains implement it
        pass


class _CrashOnConnectClient:
    """connect() raises immediately — the D021 scenario (missing CLI, bad cwd)."""

    provider = "anthropic"

    def __init__(self):
        self.connected = False

    async def connect(self, prompt=None):
        raise RuntimeError("brain CLI not found")

    async def query(self, text):  # pragma: no cover - never reached
        pass

    async def receive_response(self):  # pragma: no cover - never reached
        yield

    async def disconnect(self):
        pass


@pytest.fixture
def hermetic_session(bobi_install, monkeypatch):
    """A Session with the event subscription stubbed out.

    Registration is a network call with a background retry thread; nothing in
    these tests exercises it, and leaving it live would make the timing
    assertions measure the event server instead of the code under test.
    """
    monkeypatch.setattr("bobi.session.load_resumable_session_id", lambda *a, **k: "")
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)

    def _make(name):
        s = Session(name=name, cwd=str(bobi_install.repo_path))
        s._start_subscription = lambda: None
        _register(s, bobi_install)
        return s

    return _make


def test_stop_tears_down_a_session_hung_in_its_startup_turn(hermetic_session):
    """D003: stop() must end the session thread even mid-startup-turn.

    Pre-fix, ``_keep_alive`` is still None at this point, so ``stop()`` signals
    nothing, ``join(15)`` expires, and the thread survives — the assertion below
    fails with the session still alive.
    """
    session = hermetic_session("hung-startup")
    client = _HungStartupClient()
    session._make_brain_session = lambda resume=None: client

    # A short launch timeout, exactly as a phase whose startup turn overruns.
    assert session.start(startup_prompt="go", timeout=1) is False
    assert client.turn_started.wait(timeout=5), "startup turn never began"

    session.stop()

    assert session._thread is not None
    assert not session._thread.is_alive(), (
        "stop() left the session thread running with the startup turn in "
        "flight — the brain subprocess leaks for the life of the process"
    )
    assert not session.is_alive()
    # The thread dying is only half of it: the leak D003 describes is the brain
    # subprocess. _run's own finally never runs on this path (it guards the
    # keep-alive wait, which we never reached), so teardown has to disconnect.
    assert client.disconnected, "the brain client was never disconnected"
    assert session._client is None


def test_stop_is_idempotent_after_a_hung_startup(hermetic_session):
    """A second stop() on an already-torn-down session is a no-op, not a raise.

    ``run_phase_blocking`` calls ``stop()`` on both the failure and the cleanup
    path, so the teardown added for D003 has to tolerate being run twice.
    """
    session = hermetic_session("hung-startup-twice")
    session._make_brain_session = lambda resume=None: _HungStartupClient()

    assert session.start(startup_prompt="go", timeout=1) is False
    session.stop()
    session.stop()

    assert not session._thread.is_alive()


def test_start_returns_promptly_when_the_session_thread_dies(hermetic_session):
    """D021: start() must not wait out the timeout after the thread has exited.

    Pre-fix this blocks for the full ``timeout`` — the assertion below is what
    fails, not the return value.
    """
    session = hermetic_session("crash-on-connect")
    session._make_brain_session = lambda resume=None: _CrashOnConnectClient()

    started = time.monotonic()
    ready = session.start(startup_prompt="go", timeout=30)
    elapsed = time.monotonic() - started

    assert ready is False
    assert elapsed < 5, (
        f"start() waited {elapsed:.1f}s for a thread that had already died; "
        "with the 3600s launch timeouts in subagent.py this stalls dispatch "
        "for an hour"
    )
    assert session.detect_state() == "error"

    session.stop()


def test_start_still_reports_ready_when_the_thread_finishes_normally(hermetic_session):
    """The liveness check must not race a session that became ready and exited.

    A thread can set ``_ready`` and then exit before ``start()`` next looks at
    it. Liveness is checked *after* re-reading ``_ready``, so this returns True;
    a naive `is_alive()` poll would report failure for a session that started
    perfectly well.
    """
    session = hermetic_session("ready-then-gone")

    session._ready.set()
    session._thread = threading.Thread(target=lambda: None)
    session._thread.start()
    session._thread.join()

    assert session.wait_until_ready(timeout=1) is True
