"""Forensic backup and atomic metrics read-model rebuild operations."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json, try_file_lock
from bobi.metrics.collector import MetricsCollector, rebuild_database
from bobi.metrics.spool import SpoolCorruptionError
from bobi.metrics.store import connect, integrity_check, logical_snapshot


class MetricsMaintenanceBusy(RuntimeError):
    """The single-writer collector lock is currently owned elsewhere."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _acquire(lock_path: Path, *, wait: bool, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        context = try_file_lock(lock_path)
        acquired = context.__enter__()
        if acquired:
            return context
        context.__exit__(None, None, None)
        if not wait or time.monotonic() >= deadline:
            raise MetricsMaintenanceBusy("metrics collector is busy")
        time.sleep(0.05)


def create_backup_locked(metrics_root: Path) -> dict[str, object]:
    """Create and verify one consistent SQLite backup; caller holds the lock."""
    db_path = metrics_root / "metrics.db"
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backup_root = metrics_root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time_ns()}"
    backup_path = backup_root / f"metrics-{stamp}.db"
    source = connect(db_path, readonly=True)
    target = connect(backup_path)
    try:
        source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.execute("PRAGMA journal_mode = DELETE")
        snapshot = logical_snapshot(target)
        integrity = integrity_check(target)
    finally:
        source.close()
        target.close()
    if integrity != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("metrics backup failed integrity_check")
    manifest = {
        "created_at_us": time.time_ns() // 1000,
        "database": str(backup_path),
        "database_sha256": _sha256(backup_path),
        "integrity": integrity,
        "snapshot": snapshot,
    }
    manifest_path = backup_path.with_suffix(".manifest.json")
    atomic_write_json(manifest_path, manifest, sort_keys=True, fsync=True)
    return {**manifest, "manifest": str(manifest_path)}


def create_backup(
    root: Path | str,
    *,
    wait: bool = False,
    timeout: float = 60.0,
) -> dict[str, object]:
    metrics_root = Path(root).resolve() / "state" / "metrics"
    lock = _acquire(
        metrics_root / "collector", wait=wait, timeout=timeout
    )
    try:
        return create_backup_locked(metrics_root)
    finally:
        lock.__exit__(None, None, None)


def latest_verified_backup(metrics_root: Path) -> dict[str, object] | None:
    manifests = sorted(
        (metrics_root / "backups").glob("metrics-*.manifest.json"),
        reverse=True,
    )
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text())
            database = Path(str(manifest["database"]))
            if database.exists() and _sha256(database) == manifest["database_sha256"]:
                return {**manifest, "manifest": str(manifest_path)}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def _preserve_forensics(metrics_root: Path) -> Path:
    forensic = metrics_root / "forensics" / f"metrics-{time.time_ns()}"
    forensic.mkdir(parents=True, exist_ok=False)
    for suffix in ("", "-wal", "-shm"):
        source = metrics_root / f"metrics.db{suffix}"
        if source.exists():
            shutil.copy2(source, forensic / source.name)
    return forensic


def _checkpoint_standalone(path: Path) -> None:
    """Checkpoint every committed WAL page before a file-level swap or link."""
    conn = connect(path)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0 or int(row[1]) != int(row[2]):
            raise RuntimeError(f"metrics WAL checkpoint remained busy: {path}")
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()


def _collect_retained_segments(metrics_root: Path, target_path: Path) -> dict[str, int]:
    collector = MetricsCollector(target_path)
    collector.prepare_locked()
    imported = 0
    for segment in sorted((metrics_root / "spool").glob("*/*.telemetry")):
        try:
            collector.collect_segment_locked(segment)
        except SpoolCorruptionError as exc:
            raise RuntimeError(
                f"cannot rebuild from corrupt spool segment: {segment}"
            ) from exc
        imported += 1
    for archive in sorted((metrics_root / "archive").rglob("*.telemetry.gz")):
        with tempfile.NamedTemporaryFile(suffix=".telemetry", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with gzip.open(archive, "rb") as source, tmp_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            try:
                collector.collect_segment_locked(tmp_path)
            except SpoolCorruptionError as exc:
                raise RuntimeError(
                    f"cannot rebuild from corrupt archive segment: {archive}"
                ) from exc
            imported += 1
        finally:
            tmp_path.unlink(missing_ok=True)
    return {"segments_scanned": imported, "corrupt_segments": 0}


def rebuild_metrics(
    root: Path | str,
    *,
    wait: bool = False,
    timeout: float = 60.0,
    _lock_held: bool = False,
) -> dict[str, object]:
    """Build, verify, and atomically activate a fresh metrics database."""
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    lock = None
    if not _lock_held:
        lock = _acquire(metrics_root / "collector", wait=wait, timeout=timeout)
    started = time.monotonic()
    try:
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        forensic = _preserve_forensics(metrics_root)
        backup = None
        try:
            backup = create_backup_locked(metrics_root)
        except Exception:
            # A corrupt source may not be backup-readable; the raw forensic copy
            # and retained spools still allow repair without touching the agent.
            backup = None
        candidate = metrics_root / f".metrics-rebuild-{os.getpid()}-{time.time_ns()}.db"
        source_kind = "raw_events"
        segment_report = {"segments_scanned": 0, "corrupt_segments": 0}
        try:
            try:
                expected = rebuild_database(db_path, candidate)
            except Exception:
                candidate.unlink(missing_ok=True)
                source_kind = "retained_segments"
                segment_report = _collect_retained_segments(metrics_root, candidate)
                rebuilt = connect(candidate, readonly=True)
                try:
                    expected = logical_snapshot(rebuilt)
                finally:
                    rebuilt.close()
            checked = connect(candidate, readonly=True)
            try:
                if integrity_check(checked) != "ok":
                    raise RuntimeError("rebuilt metrics database failed integrity_check")
                actual = logical_snapshot(checked)
            finally:
                checked.close()
            if actual != expected:
                raise RuntimeError("rebuilt metrics logical snapshot mismatch")
            _checkpoint_standalone(candidate)
            _checkpoint_standalone(db_path)
            for suffix in ("-wal", "-shm"):
                (metrics_root / f"metrics.db{suffix}").unlink(missing_ok=True)
                Path(f"{candidate}{suffix}").unlink(missing_ok=True)
            rollback = metrics_root / (
                f".metrics-rollback-{os.getpid()}-{time.time_ns()}.db"
            )
            os.link(db_path, rollback)
            try:
                candidate.replace(db_path)
                active = connect(db_path, readonly=True)
                try:
                    active_integrity = integrity_check(active)
                    active_snapshot = logical_snapshot(active)
                finally:
                    active.close()
                if active_integrity != "ok" or active_snapshot != expected:
                    raise RuntimeError(
                        "activated metrics database verification failed"
                    )
            except Exception:
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{db_path}{suffix}").unlink(missing_ok=True)
                rollback.replace(db_path)
                raise
            else:
                rollback.unlink(missing_ok=True)
        finally:
            candidate.unlink(missing_ok=True)
        return {
            "status": "done",
            "source": source_kind,
            "database": str(db_path),
            "forensic_directory": str(forensic),
            "backup": backup,
            "snapshot": expected,
            **segment_report,
            "rebuild_latency_ms": round((time.monotonic() - started) * 1000, 3),
        }
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
