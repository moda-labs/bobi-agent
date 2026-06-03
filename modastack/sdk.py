"""Session registry — persistent tracking for all Claude Code sessions.

Every session (manager or engineer) is tracked here. Sessions persist
across restarts via a JSON registry at ~/.modastack/sessions/registry.json.
Each session wraps a ClaudeSDKClient with connect/resume/query/disconnect.
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
SESSION_DIR = Path.home() / ".modastack" / "sessions"
REGISTRY_PATH = SESSION_DIR / "registry.json"


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

    Each session gets a directory at ~/.modastack/sessions/<name>/
    containing state.json, handoff-<step>.yaml files, and log.jsonl.
    Active sessions have a live pid; completed ones remain for history.
    """

    def __init__(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def session_dir(name: str) -> Path:
        return SESSION_DIR / name

    @staticmethod
    def _state_path(name: str) -> Path:
        return SESSION_DIR / name / "state.json"

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
        """Mark a session as done — clear the pid so it's no longer active."""
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
        """List sessions with a live process, reaping dead ones."""
        result = []
        for d in SESSION_DIR.iterdir():
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
        for d in SESSION_DIR.iterdir():
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
        return SESSION_DIR / name / f"handoff-{step}.yaml"

    @staticmethod
    def log_path(name: str) -> Path:
        p = SESSION_DIR / name / "log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


_registry: SessionRegistry | None = None


def get_registry() -> SessionRegistry:
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry


def save_session_id(name: str, session_id: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / f"{name}.id").write_text(session_id)
    registry = get_registry()
    registry.update(name, session_id=session_id)


def load_session_id(name: str) -> str:
    path = SESSION_DIR / f"{name}.id"
    if path.exists():
        return path.read_text().strip()
    return ""


def log_activity(event: str, data: dict | None = None, session: str = "moda-manager") -> None:
    entry = {"event": event, "ts": time.time()}
    if data:
        entry.update(data)
    log_path = SessionRegistry.log_path(session)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
