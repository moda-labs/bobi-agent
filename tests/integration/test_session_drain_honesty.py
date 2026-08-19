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

from bobi import manager_health
from bobi.inbox import Message
from bobi.sdk import get_registry
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


@pytest.mark.timeout(240)
def test_brain_auth_failure_is_terminal_visible_and_not_acked(
    bobi_env, monkeypatch, tmp_path
):
    """The same session path handles stub and real Claude auth failures."""
    is_stub = bobi_env.env.get("BOBI_BRAIN") == "stub"
    if not is_stub:
        config_dir = tmp_path / "claude-config"
        config_dir.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-mod-292-test-key")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    session = Session(
        name="brain-auth-honesty",
        cwd=str(bobi_env.project_path),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    try:
        assert session.start(startup_prompt=None, timeout=120), \
            "session failed to start"

        acked = []
        text = "__stub__:auth" if is_stub else "Reply with exactly OK."
        msg = Message(id="auth-1", sender="e2e", text=text,
                      on_done=lambda: acked.append(True))
        session.inbox.push(msg)

        assert _wait_for(lambda: session._state == "error", 90), \
            f"session never reached error state (state={session._state})"
        entry = get_registry().get(session.name)
        assert entry is not None
        assert entry.status == "error"
        assert entry.error
        assert entry.terminal_at > 0

        block = manager_health._manager_block_from_registry(session.name)
        assert block["status"] == "error"
        assert block["error"] == entry.error
        assert block["terminal_at"] == entry.terminal_at

        time.sleep(1.0)
        assert acked == []
    finally:
        session.stop()


@pytest.mark.timeout(120)
def test_stub_credit_failure_alerts_without_acknowledging_the_event(
    bobi_env, monkeypatch
):
    """The full manager thread emits the alert without a successful turn."""
    if bobi_env.env.get("BOBI_BRAIN") != "stub":
        pytest.skip("credit exhaustion is deterministic only on the stub brain")

    posts = []
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: posts.append((topic, payload)),
    )
    session = Session(
        name="brain-credit-alert",
        cwd=str(bobi_env.project_path),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    try:
        assert session.start(startup_prompt=None, timeout=30)
        acked = []
        session.inbox.push(Message(
            id="credits-1",
            sender="e2e",
            text="__stub__:credits",
            on_done=lambda: acked.append(True),
        ))

        assert _wait_for(lambda: session._state == "error", 30)
        assert [topic for topic, _ in posts] == [
            "system/brain.credits.exhausted"
        ]
        time.sleep(0.2)
        assert acked == []
    finally:
        session.stop()
