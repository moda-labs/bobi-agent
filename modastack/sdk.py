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
    # OS pid of the process actually running this session. Set for detached
    # engineer/workflow subprocesses (and the subprocess that runs their
    # phases) so liveness can be checked and the process can be signalled to
    # cancel. 0 means "no tracked process" (legacy rows, in-process threads).
    pid: int = 0
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    # Who requested this work, for routing async results back to them.
    # Slack origin: {user_id, from, workspace, channel, thread_ts}. Empty for
    # non-Slack-originated work. Defaults empty so existing rows deserialize
    # unchanged — no registry migration needed.
    requested_by: dict = field(default_factory=dict)


class SessionRegistry:
    def __init__(self):
        self._entries: dict[str, SessionEntry] = {}
        self._removed: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not REGISTRY_PATH.exists():
            return
        try:
            data = json.loads(REGISTRY_PATH.read_text())
            for name, raw in data.items():
                self._entries[name] = SessionEntry(**raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("Corrupt session registry — starting fresh")
            self._entries = {}

    def _save(self) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        # Merge with disk state so entries written by other processes
        # (engineer subprocesses) aren't lost when we save.
        if REGISTRY_PATH.exists():
            try:
                disk = json.loads(REGISTRY_PATH.read_text())
                for name, raw in disk.items():
                    if name not in self._entries and name not in self._removed:
                        self._entries[name] = SessionEntry(**raw)
            except (json.JSONDecodeError, TypeError):
                pass
        data = {name: asdict(entry) for name, entry in self._entries.items()}
        REGISTRY_PATH.write_text(json.dumps(data, indent=2))

    def register(self, entry: SessionEntry) -> None:
        self._entries[entry.name] = entry
        self._save()

    def update(self, name: str, **kwargs) -> None:
        entry = self._entries.get(name)
        if not entry:
            return
        for k, v in kwargs.items():
            if hasattr(entry, k):
                setattr(entry, k, v)
        entry.last_activity = time.time()
        self._save()

    def remove(self, name: str) -> None:
        self._entries.pop(name, None)
        self._removed.add(name)
        self._save()

    def get(self, name: str) -> SessionEntry | None:
        return self._entries.get(name)

    def list_active(self) -> list[SessionEntry]:
        return [e for e in self._entries.values() if e.status in ("starting", "running", "idle")]

    def list_all(self) -> list[SessionEntry]:
        return list(self._entries.values())

    def get_by_role(self, role: str) -> list[SessionEntry]:
        return [e for e in self._entries.values() if e.role == role]

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
        for name, entry in self._entries.items():
            # "waiting" is an intentional suspension (e.g. awaiting human
            # approval), not a crash — leave it for the user to resume/cancel.
            if entry.status not in ("starting", "running", "idle"):
                continue
            if entry.pid and not _pid_alive(entry.pid):
                entry.status = "stale"
                entry.last_activity = time.time()
                reaped.append(name)
                continue
            if not entry.pid:
                ref_time = (entry.started_at if entry.status == "starting"
                            else entry.last_activity)
                age = time.time() - ref_time
                if age > 300:
                    entry.status = "stale"
                    entry.last_activity = time.time()
                    reaped.append(name)
        if reaped:
            self._save()
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
