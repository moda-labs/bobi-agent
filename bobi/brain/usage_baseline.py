"""Session-cumulative usage totals saved per provider session.

Claude's ``total_cost_usd`` / ``modelUsage`` and Codex's ``turn.completed``
usage count the whole provider session, and stay cumulative after a resume in
a new process: the resumed turn's total includes every turn before it. Each
brain reports one turn by subtracting the totals it saw on the previous turn.
Saving those totals here, keyed by session id, lets the first turn after a
resume do the same subtraction instead of reporting the whole session again.

A fresh session saves a zero baseline as soon as its id is known, so a first
turn that fails before any usage still leaves one. With nothing saved (no bound
runtime root, a session started before this file existed, an unreadable file)
the first resumed turn reports no usage. That under-reports one turn;
re-counting the session would over-report all of them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from bobi import fsutil, paths

log = logging.getLogger(__name__)

# Provider session and thread ids are UUIDs; anything else is not a file name.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# A baseline idle this long is dropped once no saved session id refers to it,
# rather than keeping one small file per provider session forever.
_KEEP_SECONDS = 30 * 24 * 60 * 60
_pruned = False


def _path(brain: str, session_id: str) -> Path | None:
    root = paths.bound_root()
    if root is None or not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    return paths.state_path(root) / "usage_baselines" / f"{brain}-{session_id}.json"


def load(brain: str, session_id: str) -> dict[str, Any] | None:
    """The totals saved after this session's last turn, or None."""
    path = _path(brain, session_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(brain: str, session_id: str, totals: dict[str, Any]) -> None:
    """Best effort: a failed write removes the older baseline, so the next
    resume reports no first turn instead of subtracting a stale total.

    One whole-file replace with no read before it, so no lock: concurrent
    writers of one session (not a supported mode) leave the last turn's totals.
    """
    path = _path(brain, session_id)
    if path is None:
        return
    try:
        fsutil.atomic_write_json(path, totals)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("could not save usage baseline for %s session: %s", brain, exc)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _prune_once(path.parent)


def _prune_once(directory: Path) -> None:
    global _pruned
    if _pruned:
        return
    _pruned = True
    cutoff = time.time() - _KEEP_SECONDS
    try:
        live = {path.read_text(encoding="utf-8").strip()
                for path in paths.sessions_dir(paths.bound_root()).rglob("*.id")}
    except OSError:
        return
    for stale in directory.glob("*.json"):
        session_id = stale.stem.split("-", 1)[-1]
        try:
            if session_id not in live and stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue
