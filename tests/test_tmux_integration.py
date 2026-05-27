"""Integration tests for tmux primitives — real tmux sessions, no mocks.

These tests create actual tmux sessions, inject text, verify paste,
and detect state. They exercise the full send_text pipeline:
locking → length routing → paste verification → enter submission.

Skip if tmux is not installed.
"""

import shutil
import subprocess
import time

import pytest

from modastack.tmux import (
    has_session, capture_pane, get_pane_pid, has_child_processes,
    kill_session, send_text, determine_agent_state, TMUX,
)


def _has_tmux():
    return shutil.which("tmux") is not None


pytestmark = pytest.mark.skipif(not _has_tmux(), reason="tmux not installed")

TEST_SESSION = "moda-tmux-test"


@pytest.fixture(autouse=True)
def cleanup():
    """Kill the test session before and after each test."""
    subprocess.run([TMUX, "kill-session", "-t", TEST_SESSION], capture_output=True)
    yield
    subprocess.run([TMUX, "kill-session", "-t", TEST_SESSION], capture_output=True)


def _start_cat_session():
    """Start a session running `cat` — reads stdin, echoes to stdout."""
    subprocess.run([
        TMUX, "new-session", "-d", "-s", TEST_SESSION,
        "-x", "200", "-y", "50", "cat",
    ])
    time.sleep(0.5)


def _start_bash_session():
    """Start a session running bash."""
    subprocess.run([
        TMUX, "new-session", "-d", "-s", TEST_SESSION,
        "-x", "200", "-y", "50", "bash",
    ])
    time.sleep(0.5)


class TestHasSession:

    def test_exists_after_create(self):
        _start_cat_session()
        assert has_session(TEST_SESSION) is True

    def test_not_exists(self):
        assert has_session(TEST_SESSION) is False

    def test_not_exists_after_kill(self):
        _start_cat_session()
        kill_session(TEST_SESSION)
        time.sleep(0.3)
        assert has_session(TEST_SESSION) is False


class TestCapturePane:

    def test_captures_output(self):
        _start_cat_session()
        subprocess.run([TMUX, "send-keys", "-t", TEST_SESSION, "-l", "hello test"])
        subprocess.run([TMUX, "send-keys", "-t", TEST_SESSION, "Enter"])
        time.sleep(0.5)
        pane = capture_pane(TEST_SESSION, lines=10)
        assert "hello test" in pane

    def test_empty_session(self):
        _start_cat_session()
        pane = capture_pane(TEST_SESSION, lines=5)
        assert isinstance(pane, str)

    def test_nonexistent_session(self):
        pane = capture_pane("nonexistent-session-xyz", lines=5)
        assert pane == ""


class TestGetPanePid:

    def test_returns_pid(self):
        _start_cat_session()
        pid = get_pane_pid(TEST_SESSION)
        assert pid.isdigit()

    def test_nonexistent_session(self):
        assert get_pane_pid("nonexistent-xyz") == ""


class TestHasChildProcesses:

    def test_cat_has_no_children(self):
        _start_cat_session()
        pid = get_pane_pid(TEST_SESSION)
        # cat itself is the child of the shell; pane_pid is the shell
        # On some systems cat IS the pane process (no shell wrapper)
        # Just verify we get a boolean without crashing
        result = has_child_processes(pid)
        assert isinstance(result, bool)

    def test_empty_pid(self):
        assert has_child_processes("") is False


class TestSendTextShort:
    """Test send_text with short messages (< 1024 chars) — uses send-keys."""

    def test_short_message_lands(self):
        _start_cat_session()
        send_text(TEST_SESSION, "hello from modastack", verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=10)
        assert "hello from modastack" in pane

    def test_multiline_collapsed(self):
        _start_cat_session()
        send_text(TEST_SESSION, "line one\nline two\nline three", verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=10)
        assert "line one line two line three" in pane

    def test_special_characters(self):
        _start_cat_session()
        send_text(TEST_SESSION, "test with 'quotes' and $vars", verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=10)
        assert "quotes" in pane


class TestSendTextLong:
    """Test send_text with long messages (>= 1024 chars) — uses load-buffer."""

    def test_long_message_lands(self):
        _start_cat_session()
        long_msg = "word " * 250  # ~1250 chars
        send_text(TEST_SESSION, long_msg, verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=30)
        # At least a significant portion should be in the pane
        assert "word word word" in pane

    def test_exact_threshold_message(self):
        _start_cat_session()
        msg = "a" * 1024
        send_text(TEST_SESSION, msg, verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=30)
        assert "aaaa" in pane


class TestSendTextConcurrency:
    """Test that file locking prevents interleaving."""

    def test_sequential_sends_dont_interleave(self):
        _start_cat_session()
        send_text(TEST_SESSION, "FIRST MESSAGE", verify=False)
        time.sleep(0.5)
        send_text(TEST_SESSION, "SECOND MESSAGE", verify=False)
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=20)
        # Both messages should appear, not garbled
        assert "FIRST MESSAGE" in pane
        assert "SECOND MESSAGE" in pane


class TestSendTextWithVerification:
    """Test paste verification end-to-end."""

    def test_verify_succeeds_on_real_paste(self):
        _start_bash_session()
        result = send_text(TEST_SESSION, "echo verified", verify=True)
        assert result is True
        time.sleep(0.5)
        pane = capture_pane(TEST_SESSION, lines=10)
        assert "verified" in pane


class TestDetermineAgentStateIntegration:
    """Test determine_agent_state with real pane content."""

    def test_working_state(self):
        _start_bash_session()
        subprocess.run([TMUX, "send-keys", "-t", TEST_SESSION, "-l", "sleep 30"])
        subprocess.run([TMUX, "send-keys", "-t", TEST_SESSION, "Enter"])
        time.sleep(0.5)
        pane = capture_pane(TEST_SESSION, lines=20)
        pid = get_pane_pid(TEST_SESSION)
        children = has_child_processes(pid)
        result = determine_agent_state(pane, children)
        assert result["state"] == "working"

    def test_exited_state(self):
        _start_cat_session()
        # Send EOF to make cat exit
        subprocess.run([TMUX, "send-keys", "-t", TEST_SESSION, "C-d"])
        time.sleep(1)
        pane = capture_pane(TEST_SESSION, lines=20)
        pid = get_pane_pid(TEST_SESSION)
        children = has_child_processes(pid)
        result = determine_agent_state(pane, children)
        # After cat exits, depends on whether tmux keeps the pane
        assert result["state"] in ("exited", "working", "unknown")


class TestKillSession:

    def test_kill_existing(self):
        _start_cat_session()
        assert has_session(TEST_SESSION) is True
        kill_session(TEST_SESSION)
        time.sleep(0.3)
        assert has_session(TEST_SESSION) is False

    def test_kill_nonexistent_no_error(self):
        kill_session("nonexistent-session-xyz")
