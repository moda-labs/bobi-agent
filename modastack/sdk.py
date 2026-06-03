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
ACTIVITY_DIR = Path.home() / ".modastack" / "manager"


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


ACTIVE_DIR = SESSION_DIR / "active"


class SessionRegistry:
    """File-per-worker registry. Each active worker owns one JSON file.

    No shared state, no merge logic. A worker writes its file on start
    and deletes it on exit. ``modastack status`` lists the directory.
    """

    def __init__(self):
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _path(name: str) -> Path:
        return ACTIVE_DIR / f"{name}.json"

    def register(self, entry: SessionEntry) -> None:
        path = self._path(entry.name)
        path.write_text(json.dumps(asdict(entry), indent=2))

    def update(self, name: str, **kwargs) -> None:
        path = self._path(name)
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

    def remove(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def get(self, name: str) -> SessionEntry | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return SessionEntry(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def list_active(self) -> list[SessionEntry]:
        """List active workers, reaping any whose process has died."""
        result = []
        for path in ACTIVE_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                entry = SessionEntry(**data)
            except (json.JSONDecodeError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if entry.status not in ("starting", "running", "idle"):
                continue
            if entry.pid and not _pid_alive(entry.pid):
                path.unlink(missing_ok=True)
                continue
            result.append(entry)
        return result

    def list_all(self) -> list[SessionEntry]:
        result = []
        for path in ACTIVE_DIR.glob("*.json"):
            try:
                result.append(SessionEntry(**json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    def get_by_role(self, role: str) -> list[SessionEntry]:
        return [e for e in self.list_all() if e.role == role]


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
    log_dir = ACTIVITY_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {"event": event, "ts": time.time()}
    if data:
        entry.update(data)
    log_path = log_dir / f"{session}.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
