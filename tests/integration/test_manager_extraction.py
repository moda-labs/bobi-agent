"""Integration test: inject a consultation prompt into a real Claude Code
session and verify the response extraction works.

Run manually: pytest tests/integration/test_manager_extraction.py -s
Requires: tmux, claude CLI
"""

import json
import shutil
import subprocess
import time

import pytest

from modastack.workflow.engine import (
    _extract_tagged_response,
    _extract_manager_response,
    _extract_manager_response_excluding,
    _read_last_assistant_response,
)

TMUX = shutil.which("tmux") or "tmux"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
SESSION_NAME = "test-manager-extraction"


def _session_exists() -> bool:
    return subprocess.run(
        [TMUX, "has-session", "-t", SESSION_NAME],
        capture_output=True,
    ).returncode == 0


def _capture(lines: int = 200) -> str:
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", SESSION_NAME, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return result.stdout


def _send(text: str):
    collapsed = " ".join(text.splitlines())
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "-l", collapsed])
    time.sleep(1)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])


def _wait_for_idle(timeout: int = 120) -> bool:
    """Wait until the session shows an empty ❯ prompt (truly idle).

    The key check: the last ❯ line must be empty (just ❯ with no text after it),
    and bypass permissions must be visible. This avoids false positives when
    Claude is still thinking (the prompt line echoes the injected text).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = _capture(20)
        lines = [l for l in raw.splitlines() if l.strip()]
        if not lines:
            time.sleep(3)
            continue

        has_bypass = any("bypass permissions" in l or "⏵⏵" in l for l in lines[-3:])
        if not has_bypass:
            time.sleep(3)
            continue

        # Find the last ❯ line — must be empty (idle prompt, not echo of injected text)
        for line in reversed(lines):
            stripped = line.strip()
            if "❯" in stripped and "bypass" not in stripped:
                # Empty prompt = just "❯" or "❯ " (nothing else)
                after_prompt = stripped.split("❯", 1)[1].strip()
                if not after_prompt:
                    return True
                break

        time.sleep(3)
    return False


@pytest.fixture(scope="module")
def claude_session():
    """Start a Claude Code session for testing, yield, then clean up."""
    if _session_exists():
        subprocess.run([TMUX, "kill-session", "-t", SESSION_NAME], capture_output=True)

    subprocess.run([
        TMUX, "new-session", "-d", "-s", SESSION_NAME,
        "-x", "200", "-y", "50",
        CLAUDE, "--dangerously-skip-permissions",
    ])

    # Wait for Claude to be ready (startup has placeholder text in prompt)
    deadline = time.monotonic() + 60
    ready = False
    while time.monotonic() < deadline:
        raw = _capture(10)
        if "bypass permissions" in raw and "❯" in raw:
            ready = True
            break
        time.sleep(3)
    assert ready, "Claude session failed to start"

    yield SESSION_NAME

    subprocess.run([TMUX, "kill-session", "-t", SESSION_NAME], capture_output=True)


PREAMBLE = (
    "[WORKFLOW ENGINE CONSULTATION] "
    "The workflow engine is asking for your reasoning. "
    "You have full freedom to use tools for research. "
    "But do NOT take orchestration actions: no spawning sessions, "
    "no injecting into engineers, no posting to Slack, no moving "
    "tickets. The engine handles all orchestration. "
    "IMPORTANT: Wrap your final answer in <workflow-response></workflow-response> tags. "
    "Only the text inside these tags will be used. --- "
)


def test_jsonl_extraction(claude_session):
    """Primary test: inject a consultation and read response from JSONL."""
    prompt = (
        'Issue #99 "Add unit tests" has been assigned. '
        'Draft a brief Slack pickup message (1-2 sentences). '
        'Output ONLY the message text, nothing else.'
    )
    _send(PREAMBLE + prompt)

    assert _wait_for_idle(timeout=120), "Manager did not respond in time"

    # Primary extraction: JSONL (pass test session name)
    jsonl_response = _read_last_assistant_response(session_name=SESSION_NAME)
    print(f"\nJSONL extraction: '{jsonl_response}'")

    assert jsonl_response, "JSONL extraction returned empty"
    assert len(jsonl_response) > 10, f"Response too short: '{jsonl_response}'"
    assert "WORKFLOW ENGINE" not in jsonl_response, f"Preamble leaked: '{jsonl_response}'"
    assert "orchestration" not in jsonl_response, f"Preamble leaked: '{jsonl_response}'"
    assert "do NOT take" not in jsonl_response, f"Preamble leaked: '{jsonl_response}'"


def test_jsonl_after_second_prompt(claude_session):
    """Verify JSONL extraction returns the LATEST response, not a stale one."""
    _send(PREAMBLE + 'Reply with exactly: "FIRST RESPONSE"')
    assert _wait_for_idle(timeout=120), "First prompt timed out"
    first = _read_last_assistant_response(session_name=SESSION_NAME)
    print(f"\nFirst: '{first}'")

    _send(PREAMBLE + 'Reply with exactly: "SECOND RESPONSE"')
    assert _wait_for_idle(timeout=120), "Second prompt timed out"
    second = _read_last_assistant_response(session_name=SESSION_NAME)
    print(f"Second: '{second}'")

    assert "SECOND" in second.upper() or "second" in second.lower(), \
        f"Expected second response, got: '{second}'"
