"""Session registry — persistent tracking for all Claude Code sessions.

Every session (manager or agent) is tracked here. Sessions persist
across restarts via state.json files in per-session directories.
Each session wraps a ClaudeSDKClient with connect/resume/query/disconnect.

All session state lives under a selected Bobi Agent runtime root:
``<BOBI_HOME>/agents/<name>/run/state/sessions/``. Every process binds
that root explicitly — the manager through ``bobi agent <name> start``,
children via the ``root`` their spawner passes in the args blob. Nothing
resolves runtime identity from cwd.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import time
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

from bobi import paths

log = logging.getLogger(__name__)

# Terminal-status vocabulary (MDS-65 D1). A run ends in exactly one of these.
# ``done`` is retained as a backward-compatible alias for reading old records —
# new code writes the honest vocabulary so a failed/crashed run is never
# recorded as a success.
TERMINAL_COMPLETED = "completed"
TERMINAL_FAILED = "failed"
TERMINAL_CRASHED = "crashed"
# Statuses that mean "still running" — everything else is terminal/inactive.
ACTIVE_STATUSES = ("starting", "running", "idle")
# Honest-failure terminal statuses (used by the reconciler / delivery routing).
FAILED_STATUSES = (TERMINAL_FAILED, TERMINAL_CRASHED)
# Honest terminal vocabulary (``done`` kept as a legacy read alias).
TERMINAL_STATUSES = (TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED, "done")
# A session in any of these has torn down its inbox/subscription — publishing to
# it would succeed but no one would consume it (inbox.py guard).
DEAD_STATUSES = (
    "stopped", "error", "cancelled", "done",
    TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED,
)


def _resolve_cli_path() -> str:
    """Locate the ``claude`` CLI, container-safe.

    Prefer ``PATH`` — the only thing that works in the Linux container image,
    where the pinned CLI is installed on ``PATH``
    (docs/CONTAINERIZED_DEPLOYMENT.md, The image). When it isn't found, fall
    back to the Homebrew location *only* on
    macOS dev machines; on every other platform fall back to the bare name so
    exec still resolves it via ``PATH`` at spawn time rather than a
    macOS-specific absolute path that doesn't exist in the container.
    """
    found = shutil.which("claude")
    if found:
        return found
    if platform.system() == "Darwin":
        return "/opt/homebrew/bin/claude"
    return "claude"


# Resolved once at import for back-compat; get_cli_path() re-resolves on demand.
CLAUDE_CLI = _resolve_cli_path()


def compute_manifest_hash(project_path: Path | None = None) -> str:
    """Compute a stable hash of the install-manifest.json file list.

    Returns the hex digest of the sorted file-hash entries, or "" if
    no manifest exists.  Used to detect when the installed agent image
    has changed so sessions can be rotated.
    """
    import hashlib

    manifest = paths.install_manifest_path(project_path)
    try:
        data = json.loads(manifest.read_text())
        files = data.get("files", {})
    except (json.JSONDecodeError, OSError):
        return ""
    if not files:
        return ""
    content = "".join(f"{k}:{v}" for k, v in sorted(files.items()))
    return hashlib.sha256(content.encode()).hexdigest()


def check_image_rotation(session_name: str, project_path: Path) -> bool:
    """Clear a session if the installed image has changed since it was stamped.

    Returns True if the session was rotated.  Safe no-ops: no manifest,
    no prior session, no stored hash (first run after upgrade).
    """
    current_hash = compute_manifest_hash(project_path)
    if not current_hash:
        return False
    saved_id = load_session_id(session_name)
    if not saved_id:
        return False
    registry = get_registry()
    entry = registry.get(session_name)
    if not entry or not entry.image_hash or entry.image_hash == current_hash:
        return False
    log.info("Installed image changed — rotating session for %s", session_name)
    save_session_id(session_name, "")
    return True


def set_project_root(path: Path) -> None:
    """Bind the installation root (delegates to bobi.paths)."""
    paths.bind_root(path)


def get_project_root() -> Path | None:
    """The bound installation root, or None (delegates to bobi.paths)."""
    return paths.bound_root()


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
    """Walk up from *start* to find the nearest live runtime root.

    This is a liveness DETECTOR (used by the nested-start guard), not
    config resolution — config resolution is paths.resolve_root(). Returns
    the directory containing the live manager pid, or None if no ancestor
    has one. The search starts at *start* (defaulting to the bound root)
    and stops at the filesystem root.
    """
    current = (start or paths.bound_root())
    if current is None:
        return None
    current = current.resolve()
    while True:
        if _pid_file_alive(paths.manager_pid_path(current)):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _sessions_dir() -> Path:
    """Sessions directory under the bound installation root.

    The root is bound explicitly in every process (manager at start,
    children via the spawn args' `root`), so sessions always share one
    registry without probing the filesystem.
    """
    return paths.sessions_dir()


def state_dir(project_path: Path | None = None) -> Path:
    """Runtime state directory (delegates to bobi.paths)."""
    return paths.state_dir(project_path)


def get_cli_path() -> str:
    """Resolve the ``claude`` CLI path at call time (container-safe).

    Re-resolves rather than returning the import-time constant so a CLI that
    lands on ``PATH`` after import — or a test that patches the environment —
    is picked up.
    """
    return _resolve_cli_path()


@dataclass
class SessionEntry:
    name: str
    session_id: str = ""
    role: str = ""
    run_key: str = ""
    title: str = ""
    phase: str = ""
    project: str = ""
    cwd: str = ""
    status: str = "starting"
    pid: int = 0
    image_hash: str = ""
    model: str = ""
    provider: str = ""
    total_cost_usd: float = 0.0
    model_usage: dict = field(default_factory=dict)
    rotation_count: int = 0
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    requested_by: dict = field(default_factory=dict)
    # Honest terminal status + reconciler backstop (MDS-65).
    # error: terminal failure message; "" on success/while running.
    error: str = ""
    # terminal_at: when a terminal status was durably written (0.0 = not terminal).
    terminal_at: float = 0.0
    # emit_confirmed: whether the terminal lifecycle bus POST is known to have
    # landed. The reconciler re-emits terminal-but-unconfirmed runs.
    emit_confirmed: bool = False
    # timeout: the run's declared/effective timeout in seconds, persisted at
    # register so the dead-man reconciler knows each run's deadline.
    timeout: int = 0
    # reconciled_at: set when the reconciler closed a stranded run (idempotency).
    reconciled_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "SessionEntry":
        """Build from a persisted dict, ignoring fields no longer in the schema.

        State files written by older code may carry retired keys (e.g.
        ``inbox_port`` before the inbox HTTP transport was removed). Filtering
        unknown keys keeps stale sessions readable across upgrades instead of
        raising and silently dropping them.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class SessionRegistry:
    """Directory-per-session registry.

    Each session gets a directory at <run>/state/sessions/<name>/
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
        # Normalize through the dataclass so fields added in a later schema
        # version (e.g. emit_confirmed/terminal_at — MDS-65) are present and can
        # be written. Without this, update() only touches keys already in the
        # on-disk JSON, so an entry written by older code would silently DROP a
        # set to a new field — and the reconciler, unable to persist
        # emit_confirmed=True, would re-emit that completion on every wake.
        data = asdict(SessionEntry.from_dict(data))
        for k, v in kwargs.items():
            if k in data:
                data[k] = v
        data["last_activity"] = time.time()
        path.write_text(json.dumps(data, indent=2))

    def record_cost(self, name: str, cost_usd: float,
                    model: str = "", provider: str = "",
                    input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate cost and model_usage on a session entry."""
        path = self._state_path(name)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, TypeError):
            return
        data["total_cost_usd"] = data.get("total_cost_usd", 0.0) + cost_usd
        if model or provider:
            usage = data.get("model_usage", {})
            key = f"{provider}:{model}" if provider else model
            if key:
                entry = usage.get(key, {"cost_usd": 0.0, "input_tokens": 0,
                                         "output_tokens": 0})
                entry["cost_usd"] = entry.get("cost_usd", 0.0) + cost_usd
                entry["input_tokens"] = entry.get("input_tokens", 0) + input_tokens
                entry["output_tokens"] = entry.get("output_tokens", 0) + output_tokens
                usage[key] = entry
                data["model_usage"] = usage
        if model and not data.get("model"):
            data["model"] = model
        if provider and not data.get("provider"):
            data["provider"] = provider
        data["last_activity"] = time.time()
        path.write_text(json.dumps(data, indent=2))

    def mark_done(self, name: str) -> None:
        self.update(name, status="done", pid=0)

    def mark_terminal(self, name: str, status: str, *, error: str = "",
                      session_id: str | None = None, phase: str | None = None,
                      emit_confirmed: bool = False,
                      reconciled: bool = False) -> None:
        """Durably record an honest terminal status (MDS-65 RC#2/#3).

        Writes ``status`` (one of completed/failed/crashed) plus a monotonic
        ``terminal_at`` and clears the pid — synchronously to local disk, before
        and independent of any best-effort bus POST, so a swallowed lifecycle
        emit never loses the outcome. The reconciler reads ``state.json`` as the
        source of truth and re-emits when ``emit_confirmed`` is still False.
        """
        updates: dict = {"status": status, "pid": 0, "terminal_at": time.time()}
        if error:
            updates["error"] = error
        if session_id is not None:
            updates["session_id"] = session_id
        if phase is not None:
            updates["phase"] = phase
        if emit_confirmed:
            updates["emit_confirmed"] = True
        if reconciled:
            updates["reconciled_at"] = time.time()
        self.update(name, **updates)

    def get(self, name: str) -> SessionEntry | None:
        path = self._state_path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return SessionEntry.from_dict(data)
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
                entry = SessionEntry.from_dict(data)
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.status not in ("starting", "running", "idle"):
                continue
            if entry.pid and not pid_alive(entry.pid):
                # A live status with a dead pid is a crash, not a clean finish.
                # Recording it `crashed` (not the old `done`) is the core
                # honest-status fix (MDS-65 RC#2/#3): "crashes recorded as done"
                # is exactly the gtm-team bug. The reconciler then re-emits an
                # honest agent/session.failed for it (emit_confirmed is False).
                self.mark_terminal(
                    entry.name, TERMINAL_CRASHED,
                    error="agent process died without reporting a terminal status",
                )
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
                result.append(SessionEntry.from_dict(json.loads(state.read_text())))
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


def save_session_id(name: str, session_id: str, model: str | None = None) -> None:
    """Persist a session's resume id, plus the model it runs under (#617).

    ``model=None`` leaves any recorded model untouched (for callers that do
    not know it); clearing the id (``session_id=""``) clears the model record
    too, so a fresh session never inherits a stale model note.
    """
    sd = _sessions_dir()
    (sd / f"{name}.id").write_text(session_id)
    model_path = sd / f"{name}.model"
    if not session_id:
        model_path.unlink(missing_ok=True)
    elif model is not None:
        model_path.write_text(model)
    registry = get_registry()
    registry.update(name, session_id=session_id)


def load_session_id(name: str) -> str:
    path = _sessions_dir() / f"{name}.id"
    if path.exists():
        return path.read_text().strip()
    return ""


def load_resumable_session_id(name: str, model: str) -> str:
    """The saved session id, if the active brain can continue it under *model*.

    Same-model sessions always resume. A session recorded under a different
    model continues natively only when the brain supports cross-model resume
    (#642); otherwise it starts fresh - the same rule the workflow
    orchestrator applies. An empty recorded model means "ran under the
    provider default" and still counts as a model; only sessions with no
    record at all (saved before #617) resume unconditionally, and they get a
    model record on their next save.
    """
    from bobi.brain import continuation_token, get_brain

    saved_id = load_session_id(name)
    if not saved_id:
        return ""
    model_path = _sessions_dir() / f"{name}.model"
    if not model_path.exists():
        return saved_id
    recorded = model_path.read_text().strip()
    token = continuation_token(
        get_brain(), session_id=saved_id,
        from_model=recorded, to_model=model or "",
    )
    if not token:
        log.info(
            "Session %s was recorded under model %r but %r is now resolved; "
            "starting fresh.", name, recorded or "<default>",
            model or "<default>",
        )
    elif recorded != (model or ""):
        log.info(
            "Session %s continues natively from model %r to %r.",
            name, recorded or "<default>", model or "<default>",
        )
    return token


def log_activity(event: str, data: dict | None = None, session: str = "") -> None:
    entry = {"event": event, "ts": time.time()}
    if data:
        entry.update(data)
    log_path = SessionRegistry.log_path(session)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
