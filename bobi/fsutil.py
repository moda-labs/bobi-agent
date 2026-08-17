"""Crash-safe writes for durable state files.

Every file bobi must still be able to read after an abrupt death — monitor
state, the spend window, workflow runs, ``config.toml``, the setup wizard's
resume document — goes through :func:`atomic_write_text` (or its JSON
wrapper). The pattern is always the same: serialize fully, write a temp
sibling, then ``os.replace`` it over the target. ``os.replace`` is atomic on
POSIX, so a reader sees either the whole old document or the whole new one,
never a truncated prefix.

This module exists because that pattern was independently re-implemented in
six places with six different temp-file naming schemes (D092), while a
comparable set of state writers used a bare ``write_text`` and could be
truncated by exactly the crash the six copies each guarded against
individually (D034/D038/D087/Q039). Durability fixes could not propagate.
Adding a seventh copy instead of calling this helper is the thing this
module is here to prevent.

**Which crash each mode survives.** The default survives *process* death —
SIGKILL, a supervisor restart, OOM, a deploy roll — because the rename is a
single metadata operation the kernel either performed or did not. It does
NOT promise survival of *power* loss or a kernel panic, where the new file's
data may not have reached the platter before the rename did; pass
``fsync=True`` for that, at the cost of a real disk flush per write. No
caller in bobi needed power-loss durability at the time this module was
written, so ``fsync`` defaults to False and every adopted site keeps the
durability it already had.

**What replacing a file costs.** The write lands as a *new inode* renamed
over the target, so the target's mode, ownership, and symlink-ness do not
survive it: a file someone had chmod-ed to 0600 comes back at the process
umask, and a path that was a symlink becomes a regular file. Two rules
follow. A secret whose confidentiality depends on its mode must be created
with that mode rather than fixed up afterwards — see
``bobi/config.py:save_bubble_state``, which opens with ``0o600`` for exactly
this reason and deliberately does NOT use this helper. And a mode-based
write guard is not a defense against an atomic write at all: replacing a
file needs write permission on its *directory*, not on the file.

POSIX only, in the same sense the rest of bobi's runtime is: :func:`file_lock`
uses ``fcntl.flock``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# The shape _tmp_path generates; see is_atomic_temp.
_TMP_RE = re.compile(r"\..+\.\d+\.\d+\.tmp")


def _tmp_path(path: Path) -> Path:
    """A temp sibling no other writer of *path* can be using.

    Process- and call-unique on purpose. A temp path derived only from the
    target (``.{name}.tmp``) is shared by every concurrent writer, which
    turns the atomic rename into a way to publish a torn document: writer A
    can ``os.replace`` the temp file while writer B is still writing it, and
    the reader sees a complete-looking file with half of each document.
    """
    return path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")


def is_atomic_temp(path: Path | str) -> bool:
    """True for a temp sibling left by :func:`atomic_write_text`.

    The helper removes its temp file on every exit it controls, but a
    SIGKILL inside the write window is precisely the case it exists for, and
    nothing runs after one — so a crash leaves an orphan behind. That orphan
    is inert to readers (state is loaded by explicit name, and the repo's
    state globs are ``*.json``) but NOT to anything walking a tree with
    ``rglob("*")``: a content hash that counted one would report a directory
    as changed because a write crashed in it. Callers that walk trees use
    this predicate rather than re-deriving the naming convention.

    Matches the full generated shape — ``.<name>.<pid>.<ns>.tmp``, both
    numeric — not merely "hidden and ends in .tmp". A caller uses this to
    drop files from a content hash, so a loose pattern would let an
    unrelated file named ``.notes.tmp`` slip out of that hash and stop being
    checked for changes.
    """
    return _TMP_RE.fullmatch(Path(path).name) is not None


def _fsync_path(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    mkdir: bool = True,
    executable: bool = False,
    fsync: bool = False,
) -> Path:
    """Write *text* to *path* so a crash can never leave it truncated.

    Writes a temp sibling and renames it over *path*. Returns *path*.

    *mkdir* creates the parent directory first (the common case for state
    files under a run root that may not exist yet). *executable* ORs the
    execute bit onto the temp file before the rename, so a script is never
    observable at its final path in a non-executable state. *fsync* flushes
    the temp file and its directory before/after the rename — see the module
    docstring for which crash class that buys.

    The temp file is removed on any failure, so a raising write leaves no
    litter beside the target.
    """
    path = Path(path)
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_text(text, encoding=encoding)
        if executable:
            tmp.chmod(tmp.stat().st_mode | stat.S_IEXEC)
        if fsync:
            _fsync_path(tmp)
        os.replace(tmp, path)
        if fsync:
            _fsync_path(path.parent)
    finally:
        # Only ever removes something on the failure path: a successful
        # os.replace has already consumed the temp file.
        tmp.unlink(missing_ok=True)
    return path


def atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    **kwargs: Any,
) -> Path:
    """Serialize *obj* as JSON and write it via :func:`atomic_write_text`.

    Serialization happens before the temp file is opened, so an object that
    cannot be encoded raises without having touched the filesystem.
    """
    return atomic_write_text(
        path, json.dumps(obj, indent=indent, sort_keys=sort_keys), **kwargs
    )


@contextmanager
def file_lock(path: Path | str) -> Iterator[None]:
    """Hold an exclusive advisory lock covering *path*, across processes.

    The lock is taken on a companion ``<name>.lock`` file rather than on the
    target, because the target is replaced by rename on every write — a lock
    held on the old inode would protect nothing once the first writer landed.

    Advisory, so it only serializes writers that also take it. Pair it with
    :func:`atomic_write_text` for read-modify-write state (load, mutate,
    save): the atomic write alone keeps the file parseable, but only the lock
    keeps a concurrent updater's change from being overwritten.
    """
    import fcntl

    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
