"""Dead-transport honesty e2e (#818 D001/D002) - one mechanism, two brains.

A brain transport that dies mid-drain (subprocess killed, broken pipe) lands in
``_drain_turn``'s terminal branch. That branch must be HONEST about the turn:
the turn failed, so the triggering message stays un-acked (the event server
replays it after a restart, #688) and blocking-phase consumers read
``success=False`` instead of recording a crashed phase as a clean completion.

Runs on BOTH brains (``dual_brain_env``): the stub leg raises mid-turn via the
``__stub__:raise`` directive (fast lane, always); the claude leg (gated on the
CLI) drives a real session and SIGKILLs the live ``claude`` subprocess
mid-turn - the actual production failure the branch exists for.
"""

import os
import signal
import time

import pytest

from bobi.inbox import Message
from bobi.session import Session


# Bind this file's ``bobi_env`` to the dual-brain (stub + claude) variants
# (see test_manager_lifecycle for the pattern) so the test runs once per brain.
@pytest.fixture
def bobi_env(dual_brain_env):
    return dual_brain_env


def _wait_for(cond, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.timeout(240)
def test_dead_transport_mid_turn_is_not_acked_and_reads_as_error(bobi_env):
    is_stub = bobi_env.env.get("BOBI_BRAIN") == "stub"

    session = Session(
        name="drain-honesty",
        cwd=str(bobi_env.project_path),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    try:
        assert session.start(startup_prompt=None, timeout=120), \
            "session failed to start"

        acked = []
        if is_stub:
            text = "__stub__:raise:transport-gone"
        else:
            # Any in-flight turn works - the CLI subprocess is killed the
            # moment the drain starts; ask for a long reply so the turn cannot
            # complete before the kill lands.
            text = ("Write a detailed 500-word summary of the history of "
                    "distributed consensus algorithms.")
        msg = Message(id="dt-1", sender="e2e", text=text,
                      on_done=lambda: acked.append(True))
        session.inbox.push(msg)

        if not is_stub:
            assert _wait_for(lambda: session._state == "working", 60), \
                "turn never started"
            transport = getattr(session._client._client, "_transport", None)
            proc = getattr(transport, "_process", None)
            assert proc is not None, "could not locate the claude CLI subprocess"
            os.kill(proc.pid, signal.SIGKILL)

        assert _wait_for(lambda: session._state == "error", 90), \
            f"session never reached error state (state={session._state})"
        # The dead turn reads as a failed turn to its consumers - this flag is
        # what run_phase_blocking builds AgentResult.success from (D002).
        assert session._last_is_error is True
        # And the triggering message is NOT acked, so the event server replays
        # it after the supervisor restarts the process (D001, #688). Give the
        # inbox loop a beat to prove the ack is skipped, not merely pending.
        time.sleep(1.0)
        assert acked == []
    finally:
        session.stop()


@pytest.mark.timeout(300)
def test_stop_during_startup_turn_tears_down_the_brain(bobi_env):
    """stop() must kill a session still inside its startup turn (D003).

    ``_keep_alive`` is only created *after* the startup drain, so ``stop()``
    guarded on ``if self._keep_alive:`` and silently no-oped: the join expired,
    the session thread kept running with a connected brain, and the CLI
    subprocess outlived the process that spawned it - a leaked agent per
    timed-out phase, burning tokens while the phase was reported as "session
    failed to start".

    The claude leg is the one that matters here: it asserts the *real* CLI
    subprocess is gone afterwards, which is the actual leak. The stub leg keeps
    the mechanism covered on the fast lane.
    """
    is_stub = bobi_env.env.get("BOBI_BRAIN") == "stub"
    if is_stub:
        startup = "__stub__:hang"
    else:
        # Long enough that the startup turn is still streaming when stop()
        # lands, so we are genuinely interrupting an in-flight turn.
        startup = ("Write a detailed 2000-word essay on the history of "
                   "distributed consensus algorithms.")

    session = Session(
        name="stop-during-startup",
        cwd=str(bobi_env.project_path),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    # start() gives up while the startup turn is still in flight.
    assert session.start(startup_prompt=startup, timeout=20) is False
    assert session._thread.is_alive(), "session thread already gone"

    brain_pid = None
    if not is_stub:
        transport = getattr(session._client._client, "_transport", None)
        proc = getattr(transport, "_process", None)
        assert proc is not None, "could not locate the claude CLI subprocess"
        brain_pid = proc.pid
        assert _pid_alive(brain_pid), "claude CLI died on its own"

    session.stop()

    assert not session._thread.is_alive(), (
        "session thread outlived stop() - the agent and its brain leak"
    )
    assert session.detect_state() == "stopped"

    if brain_pid is not None:
        assert _wait_for(lambda: not _pid_alive(brain_pid), 60), (
            f"claude CLI subprocess {brain_pid} survived stop()"
        )
