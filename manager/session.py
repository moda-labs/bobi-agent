"""Persistent manager tmux session.

The manager runs as a long-lived interactive Claude Code session.
Events are injected as messages via tmux send-keys.
State is tracked via Claude Code hooks (UserPromptSubmit, Stop)
that write to ~/.modastack/manager/activity.jsonl.
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
ACTIVITY_LOG = Path.home() / ".modastack" / "manager" / "activity.jsonl"


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


def _read_last_activity() -> dict | None:
    """Read the most recent entry from the activity log."""
    if not ACTIVITY_LOG.exists():
        return None
    try:
        with open(ACTIVITY_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            chunk_size = min(1024, size)
            f.seek(-chunk_size, 2)
            lines = f.read().decode().strip().splitlines()
            if lines:
                return json.loads(lines[-1])
    except Exception:
        return None
    return None


def _activity_line_count() -> int:
    """Count lines in the activity log."""
    if not ACTIVITY_LOG.exists():
        return 0
    try:
        with open(ACTIVITY_LOG) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _clear_activity_log() -> None:
    """Truncate the activity log on startup to avoid stale state."""
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    ACTIVITY_LOG.write_text("")


def start_or_resume(cwd: str = None) -> bool:
    """Start a new manager session or resume an existing one.

    Returns True if the session is ready.
    """
    if _session_exists():
        last = _read_last_activity()
        if last and last.get("event") == "Stop":
            log.info("Manager session already running and idle")
            return True

        if last and last.get("event") == "UserPromptSubmit":
            log.info("Manager session already running and working")
            return True

        # Session exists but no activity — needs startup prompt
        log.info("Manager session exists but no activity — injecting startup")
        _clear_activity_log()
        _inject_startup_prompt()
        if not _wait_for_prompt_accepted():
            log.error("Failed to inject startup prompt")
            return False
        if not _wait_for_turn_complete():
            log.warning("Startup injection accepted but turn didn't complete — continuing anyway")
        _capture_session_id()
        log.info("Manager session prompted and ready")
        return True

    if not cwd:
        # Always start in the modastack repo — hooks are installed there
        repo_root = Path(__file__).parent.parent
        if repo_root.exists():
            cwd = str(repo_root)
        else:
            config = GlobalConfig.load()
            cwd = str(config.repos[0]) if config.repos else str(Path.home())

    saved_id = _get_saved_session_id()
    _clear_activity_log()

    if saved_id:
        cmd = [CLAUDE, "--resume", saved_id, "--dangerously-skip-permissions"]
        log.info(f"Resuming manager session {saved_id}")
    else:
        cmd = [CLAUDE, "--dangerously-skip-permissions", "--name", "modastack-manager"]
        log.info("Starting new manager session")

    subprocess.run([
        TMUX, "new-session",
        "-d", "-s", SESSION_NAME,
        "-x", "200", "-y", "50",
    ] + cmd, cwd=cwd)

    # Wait for Claude Code to be ready (tmux pane exists and process is running)
    for _ in range(30):
        time.sleep(1)
        if _session_exists():
            break
    else:
        log.error("Manager tmux session failed to start")
        return False

    if not saved_id:
        # Give Claude Code a moment to initialize before injecting
        time.sleep(3)
        _inject_startup_prompt()
        if not _wait_for_prompt_accepted():
            log.error("Failed to inject startup prompt into new session")
            return False
        if not _wait_for_turn_complete():
            log.warning("Startup turn didn't complete — continuing anyway")

    _capture_session_id()
    log.info("Manager session ready")
    return True


def _inject_startup_prompt() -> None:
    """Write the startup prompt to a file and inject a read instruction."""
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

    _send_keys(
        f"Read and internalize {startup_path}. It contains your full instructions. "
        f"Read it now, then post a startup message to Slack."
    )


def _wait_for_prompt_accepted(timeout: int = 60, max_retries: int = 3) -> bool:
    """Wait for a UserPromptSubmit event, retrying send-keys if needed."""
    for attempt in range(1, max_retries + 1):
        inject_time = time.time()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            time.sleep(1)
            last = _read_last_activity()
            if last and last.get("event") == "UserPromptSubmit":
                event_ts = last.get("ts", 0)
                if event_ts >= inject_time - 5:
                    log.debug(f"Prompt accepted (attempt {attempt})")
                    return True

        log.warning(f"No UserPromptSubmit after {timeout}s (attempt {attempt}/{max_retries})")
        if attempt < max_retries:
            _inject_startup_prompt()

    return False


def _wait_for_turn_complete(timeout: int = 300) -> bool:
    """Wait for a Stop event indicating the turn is done."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        last = _read_last_activity()
        if last and last.get("event") == "Stop":
            return True
    return False


def _capture_session_id() -> None:
    """Try to extract the session ID from the tmux pane."""
    pane = capture(lines=20)
    for line in pane.splitlines():
        if "session_" in line.lower() or "ses_" in line.lower():
            import re
            match = re.search(r'(session_[a-zA-Z0-9]+|ses_[a-zA-Z0-9]+)', line)
            if match:
                _save_session_id(match.group(1))
                return
    _save_session_id(SESSION_NAME)


def _send_keys(text: str) -> bool:
    """Send text into the tmux pane. Returns True if send-keys succeeded."""
    collapsed = " ".join(text.splitlines())
    result = subprocess.run(
        [TMUX, "send-keys", "-t", SESSION_NAME, "-l", collapsed],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning(f"send-keys failed: {result.stderr.strip()}")
        return False
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", SESSION_NAME, "Enter"])
    return True


def inject(text: str) -> bool:
    """Send text into the manager session and confirm it was accepted.

    Returns True if a UserPromptSubmit event appeared after injection.
    """
    inject_time = time.time()

    if not _send_keys(text):
        return False

    # Wait for UserPromptSubmit confirmation
    for _ in range(30):
        time.sleep(1)
        last = _read_last_activity()
        if last and last.get("event") == "UserPromptSubmit":
            if last.get("ts", 0) >= inject_time - 5:
                log.debug(f"Injected and confirmed ({len(text)} chars)")
                return True

    log.warning("Injection not confirmed — no UserPromptSubmit received")
    return False


def capture(lines: int = 50) -> str:
    """Capture current pane content (for debugging/CLI only)."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", SESSION_NAME, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def detect_state() -> str:
    """Detect manager state from the activity log.

    Returns: 'waiting_input' | 'working' | 'exited' | 'unknown'
    """
    if not _session_exists():
        return "exited"

    last = _read_last_activity()
    if last is None:
        return "unknown"

    event = last.get("event")
    if event == "Stop":
        return "waiting_input"
    if event == "UserPromptSubmit":
        return "working"

    return "unknown"


def is_alive() -> bool:
    return _session_exists()
