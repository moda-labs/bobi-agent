"""Manage interactive Claude Code sessions via tmux.

Each issue gets one tmux session that persists across phases.
The daemon injects tasks and captures output instead of spawning
new processes.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path

from modastack.config import LOG_DIR
from modastack.tmux import (
    TMUX, has_session, capture_pane, get_pane_pid,
    has_child_processes, send_text, determine_agent_state,
    kill_session as _tmux_kill,
)

log = logging.getLogger(__name__)

CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
SKILLS_DIR = Path(__file__).parent.parent / "roles" / "engineer" / "process"


def _session_name(issue_id: str) -> str:
    return f"moda-{issue_id.lower()}"


def session_exists(issue_id: str) -> bool:
    return has_session(_session_name(issue_id))


SESSION_IDS_DIR = Path.home() / ".modastack" / "sessions"


def sync_main_branch(repo_path: Path) -> bool:
    """Fetch origin and reset main to match. Safe on remote boxes where main isn't edited directly."""
    ref_result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if ref_result.returncode == 0:
        default_branch = ref_result.stdout.strip().split("/")[-1]
    else:
        default_branch = "main"

    result = subprocess.run(
        ["git", "fetch", "origin"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        log.warning(f"git fetch failed in {repo_path}: {result.stderr}")
        return False

    result = subprocess.run(
        ["git", "reset", "--hard", f"origin/{default_branch}"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        log.warning(f"git reset failed in {repo_path}: {result.stderr}")
        return False

    log.info(f"Synced {repo_path.name} to origin/{default_branch}")
    return True


def cleanup_worktree(issue_id: str, repo_path: Path) -> None:
    """Remove worktree, branch, and session for a completed issue."""
    branch = f"agent/{issue_id.lower()}"
    worktree_path = repo_path / "worktrees" / issue_id.lower()

    if session_exists(issue_id):
        kill_session(issue_id)
        time.sleep(1)

    if worktree_path.exists():
        result = subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            capture_output=True, text=True, cwd=repo_path,
        )
        if result.returncode != 0:
            log.warning(f"Worktree removal failed: {result.stderr}")
        else:
            log.info(f"Removed worktree: {worktree_path}")

    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, text=True, cwd=repo_path,
    )

    saved_id_path = SESSION_IDS_DIR / f"{issue_id}.id"
    if saved_id_path.exists():
        saved_id_path.unlink()


def spawn_session(issue_id: str, cwd: str) -> bool:
    """Spawn an interactive claude session for an issue, or resume a previous one."""
    name = _session_name(issue_id)
    if session_exists(issue_id):
        log.info(f"Session {name} already exists")
        return True

    sync_main_branch(Path(cwd))

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
            subprocess.run([TMUX, "pipe-pane", "-t", name, "-o", f"cat >> {log_path}"])
            log.info(f"Session {name} ready in {cwd}")
            return True

    log.error(f"Session {name} failed to start")
    kill_session(issue_id)
    return False


def inject(issue_id: str, text: str) -> None:
    """Send text into the session with locking, length routing, and paste verification."""
    name = _session_name(issue_id)
    send_text(name, text)
    log.info(f"{issue_id}: injected {len(text)} chars")


def capture(issue_id: str, lines: int = 80) -> str:
    """Capture current pane content."""
    return capture_pane(_session_name(issue_id), lines=lines)


def detect_state(issue_id: str) -> dict:
    """Analyze the pane to determine session state.

    Collects data (pane content, process liveness) then delegates to
    the pure determine_agent_state() function for testability.
    """
    name = _session_name(issue_id)
    if not has_session(name):
        return {"state": "exited"}

    pane = capture_pane(name, lines=50)
    pid = get_pane_pid(name)
    children = has_child_processes(pid)

    return determine_agent_state(pane, children)


def kill_session(issue_id: str) -> None:
    name = _session_name(issue_id)
    _tmux_kill(name)
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
