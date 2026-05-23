"""Persistent manager tmux session.

The manager runs as a long-lived interactive Claude Code session.
Events are injected as messages. Responses are captured from the pane.
Sessions survive restarts via --resume.
"""

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from modastack.config import GlobalConfig

log = logging.getLogger(__name__)

TMUX = shutil.which("tmux") or "tmux"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
SESSION_NAME = "moda-manager"
SESSION_ID_PATH = Path.home() / ".modastack" / "manager" / "session_id"
MANAGER_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _session_exists() -> bool:
    result = subprocess.run(
        [TMUX, "has-session", "-t", SESSION_NAME],
        capture_output=True,
    )
    return result.returncode == 0


def _get_saved_session_id() -> str:
    if SESSION_ID_PATH.exists():
        return SESSION_ID_PATH.read_text().strip()
    return ""


def _save_session_id(session_id: str) -> None:
    SESSION_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_ID_PATH.write_text(session_id)


def start_or_resume(cwd: str = None) -> bool:
    """Start a new manager session or resume an existing one.

    Returns True if the session is ready.
    """
    if _session_exists():
        log.info("Manager session already running")
        return True

    if not cwd:
        config = GlobalConfig.load()
        cwd = str(config.repos[0]) if config.repos else str(Path.home())

    saved_id = _get_saved_session_id()

    if saved_id:
        # Resume existing session
        cmd = [CLAUDE, "--resume", saved_id, "--dangerously-skip-permissions"]
        log.info(f"Resuming manager session {saved_id}")
    else:
        # Start fresh with the manager prompt as the first message
        cmd = [CLAUDE, "--dangerously-skip-permissions", "--name", "modastack-manager"]
        log.info("Starting new manager session")

    subprocess.run([
        TMUX, "new-session",
        "-d", "-s", SESSION_NAME,
        "-x", "200", "-y", "50",
    ] + cmd, cwd=cwd)

    # Wait for claude to be ready
    for _ in range(20):
        time.sleep(1)
        state = detect_state()
        if state == "waiting_input":
            # If new session, write the prompt to a file and tell the manager to read it
            if not saved_id:
                prompt = MANAGER_PROMPT_PATH.read_text()
                config = GlobalConfig.load()
                repos = ", ".join(p.name for p in config.repos)

                startup_path = Path.home() / ".modastack" / "manager" / "startup_prompt.md"
                startup_path.parent.mkdir(parents=True, exist_ok=True)
                startup_path.write_text(
                    f"# Startup Instructions\n\n"
                    f"You are the Modastack manager. "
                    f"Slack is your primary communication channel — post status updates, ask "
                    f"questions, and reply to DMs there. Use send_slack actions or call the "
                    f"Slack API directly. Your Slack DM channel with Zach is D0B51JP1N4C. "
                    f"You are managing these repos: {repos}. "
                    f"From now on, I will send you batches of events. For each batch, "
                    f"respond with a JSON array of actions, or use tools directly. "
                    f"Start by posting a brief startup message to Slack saying you're online "
                    f"and summarizing the current state.\n\n{prompt}"
                )

                inject(
                    f"Read and internalize {startup_path}. It contains your full instructions. "
                    f"Read it now, then post a startup message to Slack."
                )
                # Wait for it to process the prompt
                for _ in range(60):
                    time.sleep(2)
                    if detect_state() == "waiting_input":
                        break

            # Capture and save the session ID
            _capture_session_id()
            log.info("Manager session ready")
            return True

    log.error("Manager session failed to start")
    return False


def _capture_session_id() -> None:
    """Try to extract the session ID from the tmux pane."""
    # The session ID appears in the Claude Code banner or can be found via the process
    # For now, use the session name as a stable identifier
    # Real session ID would come from claude's output
    pane = capture(lines=20)
    for line in pane.splitlines():
        if "session_" in line.lower() or "ses_" in line.lower():
            # Extract session ID pattern
            import re
            match = re.search(r'(session_[a-zA-Z0-9]+|ses_[a-zA-Z0-9]+)', line)
            if match:
                _save_session_id(match.group(1))
                return
    # Fallback: save the tmux session name so we know we have one running
    _save_session_id(SESSION_NAME)


def inject(text: str) -> None:
    """Send text into the manager session."""
    collapsed = " ".join(text.splitlines())
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "-l", collapsed])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])
    log.debug(f"Manager: injected {len(collapsed)} chars")


def capture(lines: int = 50) -> str:
    """Capture current pane content."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", SESSION_NAME, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return result.stdout


def detect_state() -> str:
    """Detect if the manager session is waiting for input, working, or asking a question."""
    if not _session_exists():
        return "exited"

    raw = capture(lines=10)
    lines = [l for l in raw.splitlines() if l.strip()]

    if not lines:
        return "unknown"

    all_text = raw.lower()

    # Check for bypass permissions indicator anywhere in the captured pane
    has_bypass = "bypass permissions" in all_text or "⏵⏵" in raw

    # Check for ❯ prompt (the input cursor) in last 5 non-empty lines
    has_prompt = False
    for line in reversed(lines[-5:]):
        if "❯" in line and "bypass permissions" not in line:
            has_prompt = True
            break

    if has_prompt and has_bypass:
        return "waiting_input"

    return "working"


def kill() -> None:
    subprocess.run([TMUX, "kill-session", "-t", SESSION_NAME], capture_output=True)
    log.info("Manager session killed")


def is_alive() -> bool:
    return _session_exists()
