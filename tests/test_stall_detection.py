"""Tests for stall detection: heartbeat tracking, permission detection, process liveness."""

import hashlib
import re
import time
from unittest.mock import MagicMock, patch

import pytest

from modastack.manager.events.pollers import (
    STALL_THRESHOLD_SECS,
    STUCK_THRESHOLD_SECS,
    _strip_ansi,
)


# ---------------------------------------------------------------------------
# _strip_ansi
# ---------------------------------------------------------------------------

def test_strip_ansi_removes_codes():
    assert _strip_ansi("\x1b[32mhello\x1b[0m") == "hello"


def test_strip_ansi_noop_on_clean():
    assert _strip_ansi("plain text") == "plain text"


# ---------------------------------------------------------------------------
# detect_state: permission_blocked
# ---------------------------------------------------------------------------

class _SubprocessResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _make_run_side_effect(pane_content: str, pane_pid: str = "1234", children: str = "5678"):
    """Build a subprocess.run side-effect for detect_state calls."""
    def side_effect(cmd, **kw):
        cmd_str = " ".join(cmd)
        if "has-session" in cmd_str:
            return _SubprocessResult(returncode=0)
        if "capture-pane" in cmd_str:
            return _SubprocessResult(stdout=pane_content)
        if "list-panes" in cmd_str:
            return _SubprocessResult(stdout=pane_pid)
        if "pgrep" in cmd_str:
            return _SubprocessResult(stdout=children, returncode=0 if children else 1)
        return _SubprocessResult()
    return side_effect


@patch("modastack.session.subprocess.run")
def test_permission_blocked_yn_prompt(mock_run):
    pane = "\n".join([
        "Working on something...",
        "Reading file.py",
        "  Allow Read for /path/to/file? (y/n)",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "permission_blocked"
    assert "(y/n)" in result["prompt_line"]


@patch("modastack.session.subprocess.run")
def test_permission_blocked_allow_once(mock_run):
    pane = "\n".join([
        "Working on something...",
        "Yes, allow once",
        "No, deny",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "permission_blocked"


@patch("modastack.session.subprocess.run")
def test_permission_blocked_do_you_want(mock_run):
    pane = "\n".join([
        "Something happened",
        "Do you want to proceed with this action?",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "permission_blocked"


@patch("modastack.session.subprocess.run")
def test_asking_question_takes_priority_over_permission(mock_run):
    """Numbered options (AskUserQuestion) match before permission patterns."""
    pane = "\n".join([
        "Which option?",
        "  1. Option A",
        "  2. Option B",
        "  3. Allow all",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "asking_question"


@patch("modastack.session.subprocess.run")
def test_working_state_no_permission(mock_run):
    pane = "\n".join([
        "Running tests...",
        "test_foo PASSED",
        "test_bar PASSED",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "working"


# ---------------------------------------------------------------------------
# detect_state: process liveness (exited)
# ---------------------------------------------------------------------------

@patch("modastack.session.subprocess.run")
def test_detect_state_exited_no_children(mock_run):
    pane = "\n".join(["some old output", "still here"])
    mock_run.side_effect = _make_run_side_effect(pane, children="")

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "exited"


# ---------------------------------------------------------------------------
# _poll_workers heartbeat + stall detection
# ---------------------------------------------------------------------------

class FakeBus:
    def __init__(self):
        self.events = []

    def push(self, event_type, source, data):
        self.events.append({"type": event_type, "source": source, "data": data})

    def get_events(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


def _run_poll_cycle(session_names, detect_fn, pane_content, bus, last_states, heartbeats):
    """Simulate one cycle of _poll_workers logic (extracted for testability)."""
    from modastack.manager.events.pollers import _strip_ansi, STALL_THRESHOLD_SECS, STUCK_THRESHOLD_SECS

    for session_name in session_names:
        iid = session_name.upper().replace("WORKER-", "").replace("MODA-", "")

        state_info = detect_fn(iid)
        sess_state = state_info["state"]

        if sess_state == "exited":
            bus.push("worker.process_dead", "worker", {
                "issue_id": iid,
                "session_name": session_name,
                "reason": "tmux session exists but claude process is not running",
            })
            last_states.pop(iid, None)
            heartbeats.pop(iid, None)
            continue

        state_key = f"{iid}:{sess_state}"
        if state_key != last_states.get(iid):
            last_states[iid] = state_key
            event_data = {
                "issue_id": iid,
                "session_name": session_name,
                "session_state": sess_state,
                "alive": True,
            }
            if sess_state == "permission_blocked":
                event_data["prompt_line"] = state_info.get("prompt_line", "")
            bus.push(f"worker.{sess_state}", "worker", event_data)

        content = _strip_ansi(pane_content)
        content_hash = hashlib.md5(content.encode()).hexdigest()

        now = time.monotonic()
        hb = heartbeats.get(iid)
        if hb is None or hb["hash"] != content_hash:
            heartbeats[iid] = {
                "hash": content_hash,
                "last_change": now,
                "alerted_stall": False,
                "alerted_stuck": False,
            }
        elif sess_state not in ("waiting_input", "permission_blocked"):
            idle_secs = now - hb["last_change"]
            snippet = content.strip().splitlines()[-3:] if content.strip() else []

            if idle_secs > STUCK_THRESHOLD_SECS and not hb["alerted_stuck"]:
                hb["alerted_stuck"] = True
                bus.push("worker.stuck", "worker", {
                    "issue_id": iid,
                    "session_name": session_name,
                    "idle_seconds": int(idle_secs),
                    "last_output_snippet": "\n".join(snippet),
                })
            elif idle_secs > STALL_THRESHOLD_SECS and not hb["alerted_stall"]:
                hb["alerted_stall"] = True
                bus.push("worker.stalled", "worker", {
                    "issue_id": iid,
                    "session_name": session_name,
                    "idle_seconds": int(idle_secs),
                    "last_output_snippet": "\n".join(snippet),
                })


def test_heartbeat_emits_stalled_after_threshold():
    bus = FakeBus()
    last_states = {}
    heartbeats = {}
    pane = "Working on tests...\ntest_foo PASSED"

    detect = lambda iid: {"state": "working"}

    # First cycle: establishes baseline
    _run_poll_cycle(["moda-test-1"], detect, pane, bus, last_states, heartbeats)
    assert len(bus.get_events("worker.stalled")) == 0

    # Simulate time passing beyond stall threshold
    heartbeats["TEST-1"]["last_change"] = time.monotonic() - STALL_THRESHOLD_SECS - 1

    # Second cycle: same content, should trigger stall
    _run_poll_cycle(["moda-test-1"], detect, pane, bus, last_states, heartbeats)
    stall_events = bus.get_events("worker.stalled")
    assert len(stall_events) == 1
    assert stall_events[0]["data"]["issue_id"] == "TEST-1"


def test_heartbeat_emits_stuck_after_threshold():
    bus = FakeBus()
    last_states = {}
    heartbeats = {}
    pane = "Stuck in loop...\nretrying..."

    detect = lambda iid: {"state": "working"}

    _run_poll_cycle(["moda-test-2"], detect, pane, bus, last_states, heartbeats)

    # Push past stuck threshold
    heartbeats["TEST-2"]["last_change"] = time.monotonic() - STUCK_THRESHOLD_SECS - 1

    _run_poll_cycle(["moda-test-2"], detect, pane, bus, last_states, heartbeats)
    stuck_events = bus.get_events("worker.stuck")
    assert len(stuck_events) == 1
    assert stuck_events[0]["data"]["issue_id"] == "TEST-2"
    assert stuck_events[0]["data"]["idle_seconds"] > STUCK_THRESHOLD_SECS


def test_heartbeat_dedup_alerts():
    """Stall and stuck events only fire once each per stall episode."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}
    pane = "output"

    detect = lambda iid: {"state": "working"}

    _run_poll_cycle(["moda-test-3"], detect, pane, bus, last_states, heartbeats)
    heartbeats["TEST-3"]["last_change"] = time.monotonic() - STUCK_THRESHOLD_SECS - 1

    # Run three more cycles with same content — each event fires at most once
    for _ in range(3):
        _run_poll_cycle(["moda-test-3"], detect, pane, bus, last_states, heartbeats)

    assert len(bus.get_events("worker.stuck")) == 1
    assert len(bus.get_events("worker.stalled")) <= 1  # may fire once on a subsequent cycle


def test_heartbeat_resets_on_hash_change():
    """Alert flags reset when output changes."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "working"}

    _run_poll_cycle(["moda-test-4"], detect, "output v1", bus, last_states, heartbeats)
    heartbeats["TEST-4"]["last_change"] = time.monotonic() - STALL_THRESHOLD_SECS - 1
    _run_poll_cycle(["moda-test-4"], detect, "output v1", bus, last_states, heartbeats)
    assert len(bus.get_events("worker.stalled")) == 1

    # Output changes — resets
    _run_poll_cycle(["moda-test-4"], detect, "output v2", bus, last_states, heartbeats)
    assert heartbeats["TEST-4"]["alerted_stall"] is False
    assert heartbeats["TEST-4"]["alerted_stuck"] is False


def test_no_stall_on_waiting_input():
    """Sessions in waiting_input never trigger stall events."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}
    pane = "idle at prompt"

    detect = lambda iid: {"state": "waiting_input"}

    _run_poll_cycle(["moda-test-5"], detect, pane, bus, last_states, heartbeats)
    heartbeats["TEST-5"]["last_change"] = time.monotonic() - STUCK_THRESHOLD_SECS - 1

    _run_poll_cycle(["moda-test-5"], detect, pane, bus, last_states, heartbeats)

    assert len(bus.get_events("worker.stalled")) == 0
    assert len(bus.get_events("worker.stuck")) == 0


def test_no_stall_on_permission_blocked():
    """Sessions in permission_blocked don't trigger stall — they get their own event."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}
    pane = "Allow Read (y/n)"

    detect = lambda iid: {"state": "permission_blocked", "prompt_line": "Allow Read (y/n)"}

    _run_poll_cycle(["moda-test-6"], detect, pane, bus, last_states, heartbeats)
    heartbeats["TEST-6"]["last_change"] = time.monotonic() - STUCK_THRESHOLD_SECS - 1

    _run_poll_cycle(["moda-test-6"], detect, pane, bus, last_states, heartbeats)

    assert len(bus.get_events("worker.stalled")) == 0
    assert len(bus.get_events("worker.stuck")) == 0


def test_process_dead_emitted():
    """When detect_state returns exited but tmux session exists, emit process_dead."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "exited"}

    _run_poll_cycle(["moda-test-7"], detect, "", bus, last_states, heartbeats)

    dead_events = bus.get_events("worker.process_dead")
    assert len(dead_events) == 1
    assert dead_events[0]["data"]["issue_id"] == "TEST-7"
    assert "claude process is not running" in dead_events[0]["data"]["reason"]


def test_permission_blocked_event_includes_prompt_line():
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "permission_blocked", "prompt_line": "Allow Write for /etc/passwd? (y/n)"}

    _run_poll_cycle(["moda-test-8"], detect, "pane", bus, last_states, heartbeats)

    perm_events = bus.get_events("worker.permission_blocked")
    assert len(perm_events) == 1
    assert "Allow Write" in perm_events[0]["data"]["prompt_line"]


# ---------------------------------------------------------------------------
# detect_state: "Allow all" standalone (not as numbered option)
# ---------------------------------------------------------------------------

@patch("modastack.session.subprocess.run")
def test_permission_blocked_allow_all_standalone(mock_run):
    """'Allow all' on its own (not a numbered option) triggers permission_blocked."""
    pane = "\n".join([
        "Reading configuration...",
        "Allow all",
    ])
    mock_run.side_effect = _make_run_side_effect(pane)

    from modastack.session import detect_state
    result = detect_state("TEST-1")

    assert result["state"] == "permission_blocked"
    assert "Allow all" in result["prompt_line"]


# ---------------------------------------------------------------------------
# _poll_workers: exited state clears last_states and heartbeats
# ---------------------------------------------------------------------------

def test_process_dead_clears_heartbeats_and_last_states():
    """When a session reports exited, both heartbeats and last_states are cleaned up."""
    bus = FakeBus()
    last_states = {"TEST-9": "TEST-9:working"}
    heartbeats = {"TEST-9": {"hash": "abc", "last_change": 0, "alerted_stall": False, "alerted_stuck": False}}

    detect = lambda iid: {"state": "exited"}

    _run_poll_cycle(["moda-test-9"], detect, "", bus, last_states, heartbeats)

    assert "TEST-9" not in last_states
    assert "TEST-9" not in heartbeats
    assert len(bus.get_events("worker.process_dead")) == 1


# ---------------------------------------------------------------------------
# _poll_workers: empty pane content produces empty snippet
# ---------------------------------------------------------------------------

def test_stall_with_empty_pane_content():
    """Stall detection works even when pane content is empty/whitespace."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "working"}

    # First cycle: establish baseline with empty content
    _run_poll_cycle(["moda-test-10"], detect, "   \n  \n  ", bus, last_states, heartbeats)

    # Push past stall threshold
    heartbeats["TEST-10"]["last_change"] = time.monotonic() - STALL_THRESHOLD_SECS - 1

    # Second cycle: same content (still empty), should trigger stall
    _run_poll_cycle(["moda-test-10"], detect, "   \n  \n  ", bus, last_states, heartbeats)

    stall_events = bus.get_events("worker.stalled")
    assert len(stall_events) == 1
    # snippet should be empty since content is whitespace
    assert stall_events[0]["data"]["last_output_snippet"] == ""


# ---------------------------------------------------------------------------
# _poll_workers: state change dedup (same state doesn't re-emit)
# ---------------------------------------------------------------------------

def test_state_change_dedup():
    """Same state on consecutive cycles doesn't re-emit the event."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "working"}

    _run_poll_cycle(["moda-test-11"], detect, "output", bus, last_states, heartbeats)
    _run_poll_cycle(["moda-test-11"], detect, "output changed", bus, last_states, heartbeats)

    working_events = bus.get_events("worker.working")
    assert len(working_events) == 1  # Only emitted once

    # But a state change DOES emit
    detect2 = lambda iid: {"state": "waiting_input"}
    _run_poll_cycle(["moda-test-11"], detect2, "output changed 2", bus, last_states, heartbeats)

    waiting_events = bus.get_events("worker.waiting_input")
    assert len(waiting_events) == 1


def test_process_dead_fires_each_cycle():
    """process_dead fires each cycle the dead session is still in tmux."""
    bus = FakeBus()
    last_states = {}
    heartbeats = {}

    detect = lambda iid: {"state": "exited"}

    # Three cycles with same dead session still in tmux
    for _ in range(3):
        _run_poll_cycle(["moda-test-12"], detect, "", bus, last_states, heartbeats)

    dead_events = bus.get_events("worker.process_dead")
    assert len(dead_events) == 3


def test_strip_ansi_basic():
    """ANSI regex strips basic escape sequences."""
    assert _strip_ansi("\x1b[32mgreen\x1b[0m") == "green"
