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
from modastack.tmux import (
    TMUX, has_session as _tmux_has_session,
    capture_pane as _tmux_capture, send_text,
    get_pane_pid, has_child_processes, determine_agent_state,
)

log = logging.getLogger(__name__)

CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
SESSION_NAME = "moda-manager"
SESSION_ID_PATH = Path.home() / ".modastack" / "manager" / "session_id"
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "roles" / "manager"
MANAGER_PROMPT_PATH = _ROLES_DIR / "prompt.md"
ACTIVITY_LOG = Path.home() / ".modastack" / "manager" / "activity.jsonl"


def _load_manager_prompt() -> str:
    """Load core prompt + role-specific prompt based on config."""
    core = MANAGER_PROMPT_PATH.read_text()
    role_name = "engineering"
    try:
        config = GlobalConfig.load()
        role_name = getattr(config, "manager_role", None) or "engineering"
    except Exception:
        pass
    role_path = _ROLES_DIR / f"{role_name}.md"
    if role_path.exists():
        core += "\n\n" + role_path.read_text()
    return core


def _session_exists() -> bool:
    return _tmux_has_session(SESSION_NAME)


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

    Non-blocking — creates the tmux session and injects the startup prompt,
    then returns. The event client checks manager state before each injection.
    """
    if _session_exists():
        log.info("Manager session already running")
        return True

    if not cwd:
        repo_root = Path(__file__).parent.parent
        if repo_root.exists():
            cwd = str(repo_root)
        else:
            config = GlobalConfig.load()
            cwd = str(config.repos[0]) if config.repos else str(Path.home())

    saved_id = _get_saved_session_id()

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

    # Wait for tmux session to exist
    for _ in range(10):
        time.sleep(1)
        if _session_exists():
            break
    else:
        log.error("Manager tmux session failed to start")
        return False

    if not saved_id:
        time.sleep(3)
        _inject_startup_prompt()

    log.info("Manager session started")
    return True


def _inject_startup_prompt() -> None:
    """Write the startup prompt to a file and inject a read instruction."""
    prompt = _load_manager_prompt()
    config = GlobalConfig.load()
    repos = ", ".join(p.name for p in config.repos)

    startup_path = Path.home() / ".modastack" / "manager" / "startup_prompt.md"
    startup_path.parent.mkdir(parents=True, exist_ok=True)
    startup_path.write_text(
        f"# Startup Instructions\n\n"
        f"You are the Modastack manager. "
        f"You are managing these repos: {repos}. "
        f"From now on, you will receive human messages and system event batches. "
        f"Respond naturally — the transport layer handles delivery. "
        f"Act directly using your tools.\n\n{prompt}"
    )

    _send_keys(
        f"Read and internalize {startup_path}. It contains your full instructions."
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
    """Send text into the tmux pane with locking and length routing.

    Skips paste verification — the manager session uses Claude Code's
    input prompt which doesn't reliably echo text before submission.
    """
    return send_text(SESSION_NAME, text, verify=False)


def inject(text: str) -> bool:
    """Send text into the manager session."""
    return _send_keys(text)


def capture(lines: int = 50) -> str:
    """Capture current pane content (for debugging/CLI only)."""
    return _tmux_capture(SESSION_NAME, lines=lines)


def _detect_state_from_pane() -> str:
    """Detect manager state by parsing the tmux pane output directly."""
    pane = _tmux_capture(SESSION_NAME, lines=20)
    if not pane:
        return "unknown"
    pid = get_pane_pid(SESSION_NAME)
    has_kids = has_child_processes(pid) if pid else False
    result = determine_agent_state(pane, has_kids)
    return result.get("state", "unknown")


def detect_state() -> str:
    """Detect manager state by reading the tmux pane.

    Returns: 'waiting_input' | 'working' | 'exited' | 'unknown'
    """
    if not _session_exists():
        return "exited"
    return _detect_state_from_pane()


def wait_until_ready(timeout: int = 30) -> bool:
    """Wait until the manager pane is accessible and idle.

    Used as a startup gate to prevent injection before the pane exists.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = _tmux_capture(SESSION_NAME, lines=5)
        if not pane:
            time.sleep(1)
            continue
        state = detect_state()
        if state == "waiting_input":
            return True
        time.sleep(2)
    return False


def read_last_response() -> str | None:
    """Return the manager's last assistant message, if available."""
    last = _read_last_activity()
    if last and last.get("event") == "Stop":
        return last.get("response")
    return None


def is_alive() -> bool:
    return _session_exists()
