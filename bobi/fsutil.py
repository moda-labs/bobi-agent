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


def _open_temp(tmp: Path) -> int:
    """Create *tmp* readable only by this user, and fail if it already exists.

    The content lands BEFORE the mode is settled, so creating at the umask
    default would publish the whole payload world-readable for the length of
    the write - on the very files this module exists to keep restrictive
    (``~/.codex/config.toml`` holds live MCP tokens, ``run/.env`` holds the
    credentials behind them). Starting at 0600 and widening after means the
    window exposes nothing; a target that wants a laxer mode gets it from
    :func:`_inherit_mode` before the rename.

    ``O_EXCL`` because a predictable name is the other half of that risk: it
    refuses to write through a symlink or an attacker-planted file.
    """
    return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _settle_mode(tmp: Path, path: Path) -> None:
    """Give *tmp* the mode the finished file should carry.

    ``os.replace`` swaps in the temp file's inode, so an unadjusted temp
    carries its own mode onto the target - which would silently `chmod 600`
    every file bobi writes, since :func:`_open_temp` starts there. An existing
    target keeps the mode it already had; a new one gets what a bare
    ``write_text`` would have created (the process default), so adopting this
    helper never changes how a file first appears.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_umask()
    os.chmod(tmp, mode)


def _umask() -> int:
    """Read the process umask without leaving it changed."""
    current = os.umask(0o022)
    os.umask(current)
    return current


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
        os.close(_open_temp(tmp))
        tmp.write_text(text, encoding=encoding)
        _settle_mode(tmp, path)
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
