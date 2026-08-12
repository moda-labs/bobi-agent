"""Bounded leases for sanctioned host-heavy commands."""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from bobi import paths
from bobi.fsutil import atomic_write_json, file_lock

MIN_TTL = 30.0
MAX_TTL = 3600.0
log = logging.getLogger(__name__)


def _bounded_ttl(ttl: float) -> float:
    value = float(ttl)
    if not math.isfinite(value):
        raise ValueError("expected-busy TTL must be finite")
    return min(MAX_TTL, max(MIN_TTL, value))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {"leases": {}}
    leases = data.get("leases") if isinstance(data, dict) else None
    return {"leases": leases if isinstance(leases, dict) else {}}


def _active(leases: dict, now: float) -> dict:
    active = {}
    for lease_id, raw in leases.items():
        if not isinstance(raw, dict):
            continue
        try:
            expires_at = float(raw.get("expires_at"))
        except (TypeError, ValueError):
            continue
        if expires_at > now:
            active[lease_id] = raw
    return active


def _update(root: Path, mutate, *, now: float | None = None):
    now = time.time() if now is None else float(now)
    path = paths.expected_busy_path(root)
    with file_lock(path):
        leases = _active(_load(path)["leases"], now)
        result = mutate(leases, now)
        atomic_write_json(path, {"leases": leases}, indent=None)
        return result


def acquire_lease(root: Path, *, label: str, ttl: float,
                  now: float | None = None) -> str:
    lease_id = uuid.uuid4().hex
    bounded = _bounded_ttl(ttl)

    def add(leases, current):
        leases[lease_id] = {
            "label": label or "busy command",
            "pid": os.getpid(),
            "expires_at": current + bounded,
        }
        return lease_id

    return _update(root, add, now=now)


def renew_lease(root: Path, lease_id: str, *, ttl: float,
                now: float | None = None) -> bool:
    bounded = _bounded_ttl(ttl)

    def renew(leases, current):
        lease = leases.get(lease_id)
        if lease is None:
            return False
        lease["expires_at"] = current + bounded
        lease["pid"] = os.getpid()
        return True

    return bool(_update(root, renew, now=now))


def release_lease(root: Path, lease_id: str,
                  now: float | None = None) -> None:
    def release(leases, current):
        leases.pop(lease_id, None)

    _update(root, release, now=now)


def active_lease_snapshot(root: Path, *, now: float | None = None) -> dict | None:
    now = time.time() if now is None else float(now)
    path = paths.expected_busy_path(root)
    leases = _active(_load(path)["leases"], now)
    if not leases:
        return None
    return {
        "active": True,
        "lease_count": len(leases),
        "expires_at": max(float(v["expires_at"]) for v in leases.values()),
    }


class ExpectedBusyLease:
    """Acquire, renew, and release one lease around a blocking command."""

    def __init__(self, root: Path, *, label: str, ttl: float):
        self.root = Path(root)
        self.label = label
        self.ttl = _bounded_ttl(ttl)
        self.lease_id = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self.lease_id = acquire_lease(
            self.root, label=self.label, ttl=self.ttl,
        )
        self._thread = threading.Thread(
            target=self._renew_loop,
            daemon=True,
            name="expected-busy-renewal",
        )
        self._thread.start()
        return self

    def renew(self) -> bool:
        return renew_lease(self.root, self.lease_id, ttl=self.ttl)

    def _renew_loop(self) -> None:
        interval = max(10.0, self.ttl / 3.0)
        while not self._stop.wait(interval):
            try:
                renewed = self.renew()
            except Exception:
                log.warning("expected-busy lease renewal failed", exc_info=True)
                return
            if not renewed:
                return

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.lease_id:
            try:
                release_lease(self.root, self.lease_id)
            except Exception:
                log.warning("expected-busy lease release failed", exc_info=True)


def run_command(command: tuple[str, ...]) -> int:
    try:
        return subprocess.run(list(command)).returncode
    except FileNotFoundError:
        return 127
