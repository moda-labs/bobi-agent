"""Persistent manager tmux session.

The manager runs as a long-lived interactive Claude Code session.
Events are injected as messages. Responses are captured from the pane.
Sessions survive restarts via --resume.
"""

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
        # Session exists — but has it been prompted?
        # Check if it's still at the default "Try ..." prompt (unprompted)
        if _is_unprompted():
            log.info("Manager session exists but unprompted — injecting startup")
            if not _inject_startup_prompt_with_retry():
                log.error("Failed to inject startup prompt")
                return False
            _capture_session_id()
            log.info("Manager session prompted and ready")
        else:
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
            if not saved_id:
                if not _inject_startup_prompt_with_retry():
                    log.error("Failed to inject startup prompt into new session")
                    return False

            _capture_session_id()
            log.info("Manager session ready")
            return True

    log.error("Manager session failed to start")
    return False


def _is_unprompted() -> bool:
    """Check if the session is at Claude's default idle prompt (never received input)."""
    pane = capture(lines=5)
    return 'Try "' in pane and "bypass permissions" in pane


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

    inject(
        f"Read and internalize {startup_path}. It contains your full instructions. "
        f"Read it now, then post a startup message to Slack."
    )


def _inject_startup_prompt_with_retry(max_attempts: int = 3) -> bool:
    """Inject the startup prompt, retrying if send-keys fails.

    After injection, waits for the manager to process it and verifies it's
    no longer at the idle prompt. Returns True if the startup was accepted.
    """
    for attempt in range(1, max_attempts + 1):
        _inject_startup_prompt()

        # Wait for manager to start working or finish
        for _ in range(60):
            time.sleep(2)
            state = detect_state()
            if state == "working":
                break
            if state == "waiting_input" and not _is_unprompted():
                return True
        else:
            # Timed out — check if it's still unprompted (injection never arrived)
            if _is_unprompted():
                log.warning(
                    f"Startup injection attempt {attempt}/{max_attempts} failed "
                    "— session still at idle prompt, retrying"
                )
                time.sleep(2)
                continue
            return True

        # Manager is working — wait for it to finish
        for _ in range(90):
            time.sleep(2)
            if detect_state() == "waiting_input":
                return True
        return True

    log.error(f"Startup injection failed after {max_attempts} attempts")
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


def inject(text: str) -> bool:
    """Send text into the manager session. Returns True if send-keys succeeded."""
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
    log.debug(f"Manager: injected {len(collapsed)} chars")
    return True


def capture(lines: int = 50) -> str:
    """Capture current pane content. Returns empty string if pane not found."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", SESSION_NAME, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
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


def is_alive() -> bool:
    return _session_exists()
