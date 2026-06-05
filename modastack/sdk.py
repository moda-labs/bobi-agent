"""Session registry — persistent tracking for all Claude Code sessions.

Every session (manager or engineer) is tracked here. Sessions persist
across restarts via state.json files in per-session directories.
Each session wraps a ClaudeSDKClient with connect/resume/query/disconnect.

All state lives under <repo>/.modastack/sessions/. The repo root is set
at startup by consumer.run() via set_repo_root().
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CLAUDE_CLI = shutil.which("claude") or "/opt/homebrew/bin/claude"

_repo_root: Path | None = None


def set_repo_root(path: Path) -> None:
    """Set the repo root for all session state paths."""
    global _repo_root
    _repo_root = path


def get_repo_root() -> Path | None:
    return _repo_root


def _sessions_dir() -> Path:
    """Per-repo sessions directory."""
    if _repo_root:
        d = _repo_root / ".modastack" / "sessions"
    else:
        d = Path.home() / ".modastack" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Backward compat — modules that import SESSION_DIR get the legacy path
# at import time. Runtime code should use _sessions_dir() instead.
SESSION_DIR = Path.home() / ".modastack" / "sessions"


def get_cli_path() -> str:
    return CLAUDE_CLI


@dataclass
class SessionEntry:
    name: str
    session_id: str = ""
    role: str = "engineer"
    issue_id: str = ""
    title: str = ""
    phase: str = ""
    repo: str = ""
    cwd: str = ""
    status: str = "starting"
    pid: int = 0
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    requested_by: dict = field(default_factory=dict)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionRegistry:
    """Directory-per-session registry.

    Each session gets a directory at <repo>/.modastack/sessions/<name>/
    containing state.json, handoff-<step>.yaml files, and log.jsonl.
    Active sessions have a live pid; completed ones remain for history.
    """

    def __init__(self):
        _sessions_dir()

    @staticmethod
    def session_dir(name: str) -> Path:
        return _sessions_dir() / name

    @staticmethod
    def _state_path(name: str) -> Path:
        return _sessions_dir() / name / "state.json"

    def register(self, entry: SessionEntry) -> None:
        d = self.session_dir(entry.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(asdict(entry), indent=2))

    def update(self, name: str, **kwargs) -> None:
        path = self._state_path(name)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, TypeError):
            return
        for k, v in kwargs.items():
            if k in data:
                data[k] = v
        data["last_activity"] = time.time()
        path.write_text(json.dumps(data, indent=2))

    def mark_done(self, name: str) -> None:
        self.update(name, status="done", pid=0)

    def get(self, name: str) -> SessionEntry | None:
        path = self._state_path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return SessionEntry(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def list_active(self) -> list[SessionEntry]:
        result = []
        sd = _sessions_dir()
        for d in sd.iterdir():
            if not d.is_dir():
                continue
            state = d / "state.json"
            if not state.exists():
                continue
            try:
                data = json.loads(state.read_text())
                entry = SessionEntry(**data)
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.status not in ("starting", "running", "idle"):
                continue
            if entry.pid and not _pid_alive(entry.pid):
                self.mark_done(entry.name)
                continue
            result.append(entry)
        return result

    def list_all(self) -> list[SessionEntry]:
        result = []
        sd = _sessions_dir()
        for d in sd.iterdir():
            if not d.is_dir():
                continue
            state = d / "state.json"
            if not state.exists():
                continue
            try:
                result.append(SessionEntry(**json.loads(state.read_text())))
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    def get_by_role(self, role: str) -> list[SessionEntry]:
        return [e for e in self.list_all() if e.role == role]

    @staticmethod
    def handoff_path(name: str, step: str) -> Path:
        return _sessions_dir() / name / f"handoff-{step}.yaml"

    @staticmethod
    def log_path(name: str) -> Path:
        p = _sessions_dir() / name / "log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def reap_dead(self) -> list[str]:
        """Mark active sessions whose tracked process has exited as 'stale'.

        Detached engineer/workflow subprocesses record their pid (see
        ``SessionEntry.pid``). When such a process dies without writing a
        terminal status — crash, kill -9, machine reboot — its registry row
        is left reading "running" forever. This reconciles the registry with
        reality: any active row whose pid is no longer alive becomes "stale".

        Rows with no tracked pid (0) are normally left untouched — they
        belong to in-process threads or pre-pid legacy entries we can't
        probe. However, a session stuck in "starting" with pid=0 for more
        than 5 minutes is almost certainly dead (the subprocess never
        reported its PID), so those are reaped too. Returns the names that
        were reaped.
        """
        reaped: list[str] = []
        sd = _sessions_dir()
        for d in sd.iterdir():
            if not d.is_dir():
                continue
            state = d / "state.json"
            if not state.exists():
                continue
            try:
                data = json.loads(state.read_text())
                entry = SessionEntry(**data)
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.status not in ("starting", "running", "idle"):
                continue
            if entry.pid and not _pid_alive(entry.pid):
                self.update(entry.name, status="stale")
                reaped.append(entry.name)
                continue
            if not entry.pid:
                ref_time = (entry.started_at if entry.status == "starting"
                            else entry.last_activity)
                age = time.time() - ref_time
                if age > 300:
                    self.update(entry.name, status="stale")
                    reaped.append(entry.name)
        return reaped


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists.

    ``os.kill(pid, 0)`` sends no signal but raises if the process is gone
    (ProcessLookupError) — the standard liveness probe. A PermissionError
    means the pid exists but is owned by another user, which still counts
    as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_registry: SessionRegistry | None = None


def get_registry() -> SessionRegistry:
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry


def save_session_id(name: str, session_id: str) -> None:
    sd = _sessions_dir()
    (sd / f"{name}.id").write_text(session_id)
    registry = get_registry()
    registry.update(name, session_id=session_id)


def load_session_id(name: str) -> str:
    path = _sessions_dir() / f"{name}.id"
    if path.exists():
        return path.read_text().strip()
    return ""


def log_activity(event: str, data: dict | None = None, session: str = "") -> None:
    entry = {"event": event, "ts": time.time()}
    if data:
        entry.update(data)
    log_path = SessionRegistry.log_path(session)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
