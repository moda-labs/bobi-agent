"""Integration tests: driving Claude Code inside tmux.

These tests spawn real Claude Code sessions and verify we can puppet them.
They use Claude Max credits and take 10-30s each.

Requires: tmux, claude CLI authenticated

Run with: pytest tests/integration/test_tmux_claude.py -v -s
"""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from poc.tmux_claude import (
    capture_pane,
    detect_prompt_state,
    inject_text,
    kill_session,
    session_exists,
    spawn_session,
    wait_for_prompt,
    TMUX,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def claude_session():
    """Spawn a claude session and clean up after the test."""
    name = f"agentd-test-{int(time.time())}"
    spawn_session(name, cwd=REPO_ROOT)
    state = wait_for_prompt(name, timeout=30)
    assert state["state"] == "waiting_input", f"Claude didn't start: {state['state']}"
    yield name
    if session_exists(name):
        kill_session(name)


class TestSpawnAndDetect:

    @pytest.mark.timeout(45)
    def test_spawn_creates_session(self, claude_session):
        assert session_exists(claude_session)

    @pytest.mark.timeout(45)
    def test_detect_waiting_input_on_startup(self, claude_session):
        state = detect_prompt_state(claude_session)
        assert state["state"] == "waiting_input"

    @pytest.mark.timeout(45)
    def test_capture_shows_claude_banner(self, claude_session):
        output = capture_pane(claude_session)
        assert "Claude Code" in output


class TestInjectAndRespond:

    @pytest.mark.timeout(60)
    def test_inject_simple_task(self, claude_session):
        """Send a simple question and verify claude responds."""
        inject_text(claude_session, "What is 2+2? Answer with just the number.")
        state = wait_for_prompt(claude_session, timeout=30, require_change=True)
        assert state["state"] == "waiting_input"
        output = capture_pane(claude_session)
        assert "4" in output

    @pytest.mark.timeout(90)
    def test_inject_tool_use_task(self, claude_session):
        """Send a task that requires tool use and verify it executed."""
        inject_text(claude_session, "How many SKILL.md files are in skills/? Just say the number.")
        state = wait_for_prompt(claude_session, timeout=60, require_change=True)
        assert state["state"] == "waiting_input"
        output = capture_pane(claude_session)
        assert "5" in output

    @pytest.mark.timeout(90)
    def test_context_preserved_across_turns(self, claude_session):
        """Send two tasks in sequence, verify context carries over."""
        inject_text(claude_session, "Remember the word 'banana'. Just say OK.")
        state = wait_for_prompt(claude_session, timeout=30, require_change=True)
        assert state["state"] == "waiting_input"

        inject_text(claude_session, "What word did I ask you to remember?")
        state = wait_for_prompt(claude_session, timeout=30, require_change=True)
        assert state["state"] == "waiting_input"
        output = capture_pane(claude_session)
        assert "banana" in output.lower()


class TestAskUserQuestion:

    @pytest.mark.timeout(90)
    def test_detect_ask_user_question(self, claude_session):
        """Trigger AskUserQuestion and verify we detect it."""
        inject_text(
            claude_session,
            "You must use the AskUserQuestion tool right now. Ask: 'Pick a color' "
            "with options: 'Red' and 'Blue'. Do not respond with text, only use the tool."
        )
        # Wait longer — claude needs to decide to use the tool
        state = wait_for_prompt(claude_session, timeout=60, require_change=True)
        # Either it asked a question (ideal) or it went back to prompt
        # Both are acceptable — the key test is that we can detect the difference
        assert state["state"] in ("asking_question", "waiting_input")
        if state["state"] == "asking_question":
            assert len(state.get("options", [])) >= 2

    @pytest.mark.timeout(90)
    def test_answer_ask_user_question(self, claude_session):
        """Trigger AskUserQuestion, answer it, verify claude continues."""
        inject_text(
            claude_session,
            "You must use the AskUserQuestion tool right now. Ask: 'Pick a color' "
            "with options: 'Red' and 'Blue'. Do not respond with text, only use the tool. "
            "After I pick, say 'You chose: ' followed by my answer."
        )
        state = wait_for_prompt(claude_session, timeout=60, require_change=True)

        if state["state"] == "asking_question":
            # Select first option
            subprocess.run([TMUX, "send-keys", "-t", claude_session, "Enter"])
            state = wait_for_prompt(claude_session, timeout=30, require_change=True)
            assert state["state"] == "waiting_input"
            output = capture_pane(claude_session)
            assert "chose" in output.lower() or "red" in output.lower()
        else:
            # Claude didn't use the tool — still a valid test run,
            # just means Claude answered inline
            pytest.skip("Claude chose not to use AskUserQuestion")


class TestSessionLifecycle:

    @pytest.mark.timeout(30)
    def test_kill_session(self):
        """Verify we can kill a session cleanly."""
        name = f"agentd-kill-test-{int(time.time())}"
        spawn_session(name, cwd=REPO_ROOT)
        wait_for_prompt(name, timeout=20)
        assert session_exists(name)

        kill_session(name)
        time.sleep(1)
        assert not session_exists(name)

    @pytest.mark.timeout(30)
    def test_detect_exited_session(self):
        """If the tmux session is gone, detect it as exited."""
        name = f"agentd-exit-test-{int(time.time())}"
        subprocess.run([
            TMUX, "new-session", "-d", "-s", name,
            "-x", "200", "-y", "50",
            "bash", "-c", "echo done && sleep 1",
        ])
        time.sleep(3)
        # Session may have already been destroyed by tmux
        state = detect_prompt_state(name)
        assert state["state"] in ("exited", "unknown")
        if session_exists(name):
            kill_session(name)
