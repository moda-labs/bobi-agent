"""Rock-solid tmux primitives for session injection and state detection.

Patterns adapted from imbue-ai/mngr:
- File locking to prevent concurrent injection interleaving
- Length-based routing: send-keys for short, load-buffer+paste for long
- Paste verification before pressing Enter
- Pure-function state detection (data collection separate from logic)
"""

import fcntl
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

TMUX = shutil.which("tmux") or "tmux"
LONG_MESSAGE_THRESHOLD = 1024
PASTE_VERIFY_TIMEOUT = 3.0
PASTE_VERIFY_INTERVAL = 0.3

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
LOCK_DIR = Path.home() / ".modastack" / "locks"


def has_session(session_name: str) -> bool:
    result = subprocess.run(
        [TMUX, "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def capture_pane(session_name: str, lines: int = 50) -> str:
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def get_pane_pid(session_name: str) -> str:
    result = subprocess.run(
        [TMUX, "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def has_child_processes(pane_pid: str) -> bool:
    if not pane_pid:
        return False
    result = subprocess.run(
        ["pgrep", "-P", pane_pid],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def kill_session(session_name: str) -> None:
    subprocess.run([TMUX, "kill-session", "-t", session_name], capture_output=True)


def _normalize_for_match(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower())


def _verify_paste(session_name: str, message: str) -> bool:
    """Verify the pasted text is visible in the pane.

    Checks for either tmux's "[Pasted text ..." indicator or a fuzzy
    match on the last 60 chars of the message against pane content.
    """
    deadline = time.monotonic() + PASTE_VERIFY_TIMEOUT
    while time.monotonic() < deadline:
        pane = capture_pane(session_name, lines=10)
        if "[Pasted text " in pane:
            return True
        normalized_pane = _normalize_for_match(pane)
        normalized_msg = _normalize_for_match(message)
        probe_len = min(60, len(normalized_msg))
        if probe_len == 0:
            return True
        probe = normalized_msg[-probe_len:]
        if probe in normalized_pane:
            return True
        time.sleep(PASTE_VERIFY_INTERVAL)
    return False


def _send_short(session_name: str, text: str) -> bool:
    """Send via tmux send-keys -l (fast, for messages < 1024 chars)."""
    result = subprocess.run(
        [TMUX, "send-keys", "-t", session_name, "-l", text],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _send_long(session_name: str, text: str) -> bool:
    """Send via load-buffer + paste-buffer (reliable for long messages).

    Avoids 'command too long' errors that send-keys hits with large text.
    """
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="moda-inject-"
        ) as f:
            f.write(text)
            tmp_path = f.name

        result = subprocess.run(
            [TMUX, "load-buffer", tmp_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning(f"load-buffer failed: {result.stderr.strip()}")
            return False

        result = subprocess.run(
            [TMUX, "paste-buffer", "-t", session_name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning(f"paste-buffer failed: {result.stderr.strip()}")
            return False

        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _send_enter(session_name: str) -> None:
    """Send Enter twice with delays — first submits, second confirms."""
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", session_name, "Enter"])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", session_name, "Enter"])


def send_text(session_name: str, text: str, verify: bool = True) -> bool:
    """Send text into a tmux session with locking, length routing, and verification.

    1. Acquire file lock (prevents concurrent injections from interleaving)
    2. Collapse newlines (Claude Code input is single-line)
    3. Route by length: send-keys for short, load-buffer for long
    4. Verify paste landed (fuzzy content match)
    5. Send Enter twice to submit

    Returns True if the text was sent and (optionally) verified.
    """
    collapsed = " ".join(text.splitlines())

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{session_name}.lock"

    try:
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        log.warning(f"Could not acquire lock for {session_name}: {e}")
        lock_file = None

    try:
        if len(collapsed) < LONG_MESSAGE_THRESHOLD:
            ok = _send_short(session_name, collapsed)
        else:
            log.debug(f"Long message ({len(collapsed)} chars), using load-buffer")
            ok = _send_long(session_name, collapsed)

        if not ok:
            log.warning(f"send failed for {session_name}")
            return False

        if verify and not _verify_paste(session_name, collapsed):
            log.warning(f"Paste verification failed for {session_name}, sending Enter anyway")

        _send_enter(session_name)
        return True

    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Pure-function state detection
# ---------------------------------------------------------------------------

PERMISSION_PATTERNS = [
    re.compile(r"Allow .+ \(y/n\)"),
    re.compile(r"Do you want to proceed"),
    re.compile(r"Yes, allow once"),
    re.compile(r"Allow all"),
]

OPTION_PATTERN = re.compile(r"^\s*\d+\.\s+.+")


def determine_agent_state(
    pane_content: str,
    has_children: bool,
) -> dict:
    """Determine agent state from pre-collected data. No subprocess calls.

    Args:
        pane_content: raw tmux pane capture
        has_children: whether the pane's shell has child processes

    Returns dict with 'state' key and optional detail keys.
    """
    lines = [l for l in pane_content.splitlines() if l.strip()]

    if not lines:
        if not has_children:
            return {"state": "exited"}
        return {"state": "unknown"}

    last_lines = lines[-20:]

    # Detect AskUserQuestion: numbered options
    options = [l.strip() for l in last_lines if OPTION_PATTERN.match(l)]
    if len(options) >= 2:
        question_lines = []
        for l in reversed(last_lines):
            if OPTION_PATTERN.match(l):
                continue
            stripped = l.strip()
            if stripped and "─" not in stripped and "bypass" not in stripped:
                question_lines.insert(0, stripped)
            if len(question_lines) >= 3:
                break
        return {
            "state": "asking_question",
            "question": " ".join(question_lines),
            "options": options,
        }

    # Detect permission prompts
    for line in last_lines:
        if any(p.search(line) for p in PERMISSION_PATTERNS):
            return {"state": "permission_blocked", "prompt_line": line.strip()}

    # Detect waiting for input: ❯ prompt + permissions indicator
    for line in reversed(lines[-5:]):
        if "❯" in line and "bypass permissions" not in line:
            if any("bypass permissions" in l or "⏵⏵" in l for l in lines[-3:]):
                return {"state": "waiting_input"}
            break

    # No prompt visible — check process liveness
    if not has_children:
        return {"state": "exited"}

    return {"state": "working"}
