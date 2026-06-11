"""Session registry — persistent tracking for all Claude Code sessions.

Every session (manager or engineer) is tracked here. Sessions persist
across restarts via state.json files in per-session directories.
Each session wraps a ClaudeSDKClient with connect/resume/query/disconnect.

All state lives under <runtime_root>/.modastack/sessions/. The runtime
root is the nearest ancestor (including self) whose .modastack/ has a
live manager.pid — i.e. the directory where `modastack start` was
invoked. This walk-up resolution lets sub-agents launched into child
repos register in the same registry as the director that spawned them.
When no live manager is found, the project root itself is used (the
single-project, no-manager case).
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

_project_root: Path | None = None

# Resolved sessions dir per project root. Runtime-root resolution walks the
# filesystem and probes pids; it runs on every registry op without this cache.
_sessions_dir_cache: dict[str, Path] = {}


def compute_manifest_hash(project_path: Path | None = None) -> str:
    """Compute a stable hash of the install-manifest.json file list.

    Returns the hex digest of the sorted file-hash entries, or "" if
    no manifest exists.  Used to detect when the installed agent image
    has changed so sessions can be rotated.
    """
    import hashlib

    root = project_path or _project_root
    if not root:
        return ""
    manifest = root / ".modastack" / "install-manifest.json"
    if not manifest.exists():
        return ""
    try:
        data = json.loads(manifest.read_text())
        files = data.get("files", {})
    except (json.JSONDecodeError, OSError):
        return ""
    if not files:
        return ""
    # Deterministic: sort by key and hash the concatenation
    content = "".join(f"{k}:{v}" for k, v in sorted(files.items()))
    return hashlib.sha256(content.encode()).hexdigest()


def set_project_root(path: Path) -> None:
    """Set the project root for all session state paths."""
    global _project_root
    _project_root = path
    _sessions_dir_cache.clear()


def get_project_root() -> Path | None:
    return _project_root


def pid_alive(pid: int) -> bool:
    """Whether a pid refers to a live process (signal-0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(pid_path: Path) -> int:
    """Read a pid file, returning 0 when missing or malformed."""
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return 0


def _pid_file_alive(pid_path: Path) -> bool:
    """Check whether a manager.pid file points to a running process."""
    return pid_alive(read_pid(pid_path))


def find_runtime_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* to find the nearest .modastack/ with a live manager.

    Returns the directory containing the live .modastack/, or None if no
    ancestor has one. The search starts at *start* (defaulting to
    _project_root) and stops at the filesystem root.
    """
    current = (start or _project_root)
    if current is None:
        return None
    current = current.resolve()
    while True:
        candidate = current / ".modastack" / "state" / "manager.pid"
        if _pid_file_alive(candidate):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _sessions_dir() -> Path:
    """Per-project sessions directory, resolved via walk-up to runtime root.

    If a live manager is running in an ancestor directory, sessions are
    stored under that ancestor's .modastack/sessions/ so all agents in the
    runtime are visible to each other. Falls back to the local project
    root when no ancestor manager is found. The resolution is cached per
    project root (cleared by set_project_root).
    """
    if not _project_root:
        raise RuntimeError("project root not set — call set_project_root() first")
    key = str(_project_root)
    cached = _sessions_dir_cache.get(key)
    if cached is not None:
        return cached
    runtime = find_runtime_root(_project_root) or _project_root
    d = runtime / ".modastack" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    _sessions_dir_cache[key] = d
    return d


_created_state_dirs: set[str] = set()


def state_dir(project_path: Path | None = None) -> Path:
    """Runtime state directory: <project>/.modastack/state (created on demand)."""
    root = project_path or _project_root
    if not root:
        raise RuntimeError("project root not set — call set_project_root() first")
    d = root / ".modastack" / "state"
    if str(d) not in _created_state_dirs:
        d.mkdir(parents=True, exist_ok=True)
        _created_state_dirs.add(str(d))
    return d


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
    project: str = ""
    cwd: str = ""
    status: str = "starting"
    pid: int = 0
    inbox_port: int = 0
    image_hash: str = ""
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    requested_by: dict = field(default_factory=dict)


class SessionRegistry:
    """Directory-per-session registry.

    Each session gets a directory at <project>/.modastack/sessions/<name>/
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
            if entry.pid and not pid_alive(entry.pid):
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
