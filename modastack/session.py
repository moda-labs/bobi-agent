"""Manage interactive Claude Code sessions via tmux.

Each issue gets one tmux session that persists across phases.
The daemon injects tasks and captures output instead of spawning
new processes.
"""

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from modastack.config import LOG_DIR

log = logging.getLogger(__name__)

TMUX = shutil.which("tmux") or "tmux"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
SKILLS_DIR = Path(__file__).parent.parent / "engineer" / "process"


def _session_name(issue_id: str) -> str:
    return f"moda-{issue_id.lower()}"


def session_exists(issue_id: str) -> bool:
    name = _session_name(issue_id)
    result = subprocess.run(
        [TMUX, "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


SESSION_IDS_DIR = Path.home() / ".modastack" / "sessions"


def spawn_session(issue_id: str, cwd: str) -> bool:
    """Spawn an interactive claude session for an issue, or resume a previous one."""
    name = _session_name(issue_id)
    if session_exists(issue_id):
        log.info(f"Session {name} already exists")
        return True

    # Check for a saved session ID to resume
    saved_id_path = SESSION_IDS_DIR / f"{issue_id}.id"
    cmd = [CLAUDE, "--dangerously-skip-permissions", "--name", f"moda-{issue_id.lower()}"]
    if saved_id_path.exists():
        saved_id = saved_id_path.read_text().strip()
        if saved_id:
            cmd = [CLAUDE, "--resume", saved_id, "--dangerously-skip-permissions"]
            log.info(f"Resuming session {saved_id} for {issue_id}")

    subprocess.run([
        TMUX, "new-session",
        "-d", "-s", name,
        "-x", "200", "-y", "50",
    ] + cmd, cwd=cwd)

    # Wait for claude to start
    for _ in range(15):
        time.sleep(1)
        state = detect_state(issue_id)
        if state["state"] == "waiting_input":
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / f"{name}.log"
            subprocess.run([
                TMUX, "pipe-pane", "-t", name, "-o", f"cat >> {log_path}",
            ])
            log.info(f"Session {name} ready in {cwd}")
            return True

    log.error(f"Session {name} failed to start")
    kill_session(issue_id)
    return False


def inject(issue_id: str, text: str) -> None:
    """Send text into the session as if a human typed it.

    Claude Code's input is single-line — multiline pastes get held in
    the editor buffer and don't auto-submit. We collapse newlines to
    spaces so the text arrives as one message and submits on Enter.
    The sleep between text and Enter is critical — without it, Enter
    arrives before Claude Code has buffered the text and gets swallowed.
    """
    name = _session_name(issue_id)
    collapsed = " ".join(text.splitlines())
    subprocess.run([TMUX, "send-keys", "-t", name, "-l", collapsed])
    time.sleep(1)
    subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    time.sleep(0.5)
    subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    log.info(f"{issue_id}: injected {len(collapsed)} chars")


def capture(issue_id: str, lines: int = 80) -> str:
    """Capture current pane content."""
    name = _session_name(issue_id)
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return result.stdout


def detect_state(issue_id: str) -> dict:
    """Analyze the pane to determine session state.

    Returns:
        state: 'waiting_input' | 'working' | 'asking_question' | 'exited' | 'unknown'
        question: str (if asking_question)
        options: list[str] (if asking_question)
    """
    if not session_exists(issue_id):
        return {"state": "exited"}

    raw = capture(issue_id, lines=50)
    lines = [l for l in raw.splitlines() if l.strip()]

    if not lines:
        return {"state": "unknown"}

    last_lines = lines[-20:]

    # Detect AskUserQuestion: numbered options
    option_pattern = r"^\s*\d+\.\s+.+"
    options = [l.strip() for l in last_lines if re.match(option_pattern, l)]
    if len(options) >= 2:
        question_lines = []
        for l in reversed(last_lines):
            if re.match(option_pattern, l):
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

    # Detect waiting for input: ❯ prompt + permissions indicator
    for line in reversed(lines[-5:]):
        if "❯" in line and "bypass permissions" not in line:
            if any("bypass permissions" in l or "⏵⏵" in l for l in lines[-3:]):
                return {"state": "waiting_input"}
            break

    # Check if claude process is still alive
    name = _session_name(issue_id)
    pane_pid_result = subprocess.run(
        [TMUX, "list-panes", "-t", name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pane_pid_result.returncode == 0:
        pane_pid = pane_pid_result.stdout.strip()
        children = subprocess.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True, text=True,
        )
        if children.returncode != 0 or not children.stdout.strip():
            return {"state": "exited"}

    return {"state": "working"}


def answer_question(issue_id: str, choice: int | None = None, text: str | None = None) -> None:
    """Answer an AskUserQuestion prompt.

    choice: 1-indexed option number (use arrow keys + Enter)
    text: free text to type (for "Other" option)
    """
    name = _session_name(issue_id)
    if text:
        # Select "Other" option (usually last), then type
        # Navigate to the "Type something" option
        state = detect_state(issue_id)
        options = state.get("options", [])
        # Find the "Type something" or "Other" option
        for i, opt in enumerate(options):
            if "type" in opt.lower() or "other" in opt.lower():
                for _ in range(i):
                    subprocess.run([TMUX, "send-keys", "-t", name, "Down"])
                    time.sleep(0.1)
                break
        subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
        time.sleep(0.5)
        subprocess.run([TMUX, "send-keys", "-t", name, "-l", text])
        subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    elif choice is not None:
        # Navigate to the right option and press Enter
        for _ in range(choice - 1):
            subprocess.run([TMUX, "send-keys", "-t", name, "Down"])
            time.sleep(0.1)
        subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    else:
        # Just press Enter on whatever's highlighted
        subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])

    log.info(f"{issue_id}: answered question (choice={choice}, text={text})")


def kill_session(issue_id: str) -> None:
    name = _session_name(issue_id)
    subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
    log.info(f"Session {name} killed")


def load_skill(skill_name: str) -> str:
    """Load a SKILL.md file content."""
    skill_path = SKILLS_DIR / skill_name / "SKILL.md"
    if skill_path.exists():
        return skill_path.read_text()
    return ""


def inject_skill(issue_id: str, skill_name: str, context: str = "") -> None:
    """Invoke a skill by name. Skills must be installed in .claude/skills/."""
    msg = f"/{skill_name}"
    if context:
        msg += f" {context}"
    inject(issue_id, msg)
    log.info(f"{issue_id}: invoked /{skill_name}")


def list_sessions() -> list[str]:
    """List all modastack tmux sessions. Returns issue IDs."""
    result = subprocess.run(
        [TMUX, "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [
        name.replace("moda-", "").upper()
        for name in result.stdout.strip().splitlines()
        if name.startswith("moda-")
    ]
