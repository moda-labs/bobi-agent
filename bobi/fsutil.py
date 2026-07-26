"""Durable filesystem writes - the single atomic-write path for bobi state.

Every file bobi must not lose (workflow runs, monitor state, setup resume
state, the spend window, ``config.toml``) is written the same way here:
serialize fully, write a temp sibling, then ``os.replace`` it over the
target. ``os.replace`` is atomic on POSIX, so a reader either sees the
whole previous file or the whole new one - never a truncated middle.

The threat model is a **process kill mid-write** (supervisor restart, OOM,
deploy roll, Ctrl-C), which is what turns a bare ``path.write_text()`` into
data loss: ``open(path, "w")`` truncates first, so the old content is gone
before the new content is written. It is not power-loss durability; that
would need ``fsync`` of both the file and its directory, which none of the
state files here are worth paying for on every tick.

Temp names carry pid + nanosecond stamp so two writers racing on one target
never share a temp file (a fixed temp name lets one writer's rename consume
or delete the other's, failing a write that had already succeeded).

The swap is otherwise invisible: the temp inherits an existing target's
permission mode before the rename, so replacing a file never relaxes it.

:func:`file_lock` is the companion guard for **read-modify-write** state
(load → mutate → save), where atomicity alone cannot prevent a lost update.
"""

from __future__ import annotations

import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _temp_sibling(path: Path) -> Path:
    """A unique, hidden temp path next to *path* (same filesystem → atomic rename)."""
    return path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")


def _inherit_mode(tmp: Path, path: Path) -> None:
    """Give *tmp* the mode *path* already has, so the rename does not drop it.

    ``os.replace`` swaps in the temp file's inode, so an unadjusted temp
    carries its own umask-default mode (typically 0644) onto the target - a
    silent `chmod 600` strip on files bobi fills with resolved credentials
    (``~/.codex/config.toml`` holds live MCP tokens). A target that does not
    exist yet keeps the process default, exactly as a bare ``write_text``
    would have created it.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return
    os.chmod(tmp, mode)


def atomic_write_text(path: Path | str, text: str, *,
                      encoding: str = "utf-8") -> Path:
    """Write *text* to *path* atomically. Returns *path*.

    Missing parent directories are created. An existing target's permission
    mode is preserved. If the write fails (including a KeyboardInterrupt),
    the target keeps its previous content and no temp file is left behind.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_sibling(path)
    try:
        tmp.write_text(text, encoding=encoding)
        _inherit_mode(tmp, path)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_json(path: Path | str, obj, *, indent: int | None = 2) -> Path:
    """Serialize *obj* as JSON and :func:`atomic_write_text` it to *path*.

    Serialization happens before anything touches disk, so an unserializable
    object cannot leave a half-written file.
    """
    return atomic_write_text(path, json.dumps(obj, indent=indent))


@contextmanager
def file_lock(path: Path | str) -> Iterator[None]:
    """Hold an exclusive advisory lock for *path* while the block runs.

    The lock is taken on a ``<path>.lock`` sibling rather than the state file
    itself, so locking never creates or truncates the state file, and the
    lock survives the ``os.replace`` that swaps the state file's inode.

    Advisory and best-effort: it excludes other holders of this same lock
    (threads and processes alike - ``flock`` is per open file description),
    and degrades to a no-op where ``fcntl`` is unavailable.
    """
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - fd already gone
                pass
