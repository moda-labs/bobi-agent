"""Safe archival and retention planning for local metrics state."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bobi.fsutil import try_file_lock
from bobi.metrics.rebuild import create_backup_locked, latest_verified_backup
from bobi.metrics.store import connect, integrity_check

DAY_US = 86_400 * 1_000_000
DEFAULT_RETENTION_DAYS = 30


def _old_turns_sql() -> str:
    return (
        "SELECT t.turn_id FROM turns AS t JOIN sessions AS s USING(session_id) "
        "WHERE COALESCE(s.ended_at_us,s.started_at_us)<? AND "
        "COALESCE(t.ended_at_us,t.started_at_us)<?"
    )


def _old_sessions_sql() -> str:
    return (
        "SELECT s.session_id FROM sessions AS s WHERE "
        "COALESCE(s.ended_at_us,s.started_at_us)<? AND NOT EXISTS ("
        "SELECT 1 FROM turns AS t WHERE t.session_id=s.session_id AND "
        "COALESCE(t.ended_at_us,t.started_at_us)>=?) AND NOT EXISTS ("
        "SELECT 1 FROM sessions AS child WHERE child.parent_session_id=s.session_id)"
    )


class RetentionBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class DiskPolicy:
    total_bytes: int
    free_bytes: int
    metrics_bytes: int
    budget_bytes: int
    reserve_bytes: int
    pressure: bool


def metrics_disk_bytes(metrics_root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in metrics_root.rglob("*")
        if path.is_file()
    ) if metrics_root.exists() else 0


def disk_policy(
    metrics_root: Path,
    *,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> DiskPolicy:
    usage = disk_usage(metrics_root)
    total = int(usage.total)
    free = int(usage.free)
    used = metrics_disk_bytes(metrics_root)
    budget = min(5 * 1024**3, max(256 * 1024**2, int(total * 0.05)))
    reserve = max(1024**3, int(total * 0.05))
    return DiskPolicy(
        total_bytes=total,
        free_bytes=free,
        metrics_bytes=used,
        budget_bytes=budget,
        reserve_bytes=reserve,
        pressure=used > budget or free < reserve,
    )


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _producer_is_inactive(producer: Path) -> bool:
    try:
        health = json.loads((producer / "health.json").read_text())
    except (OSError, TypeError, ValueError):
        return False
    if health.get("writer_closed") is True:
        return True
    try:
        pid = int(health.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return bool(pid) and not _process_alive(pid)


def _latest_active_segments_by_producer(spool_root: Path) -> set[Path]:
    latest = set()
    for producer in spool_root.glob("*"):
        if _producer_is_inactive(producer):
            continue
        segments = [path for path in producer.glob("*.telemetry") if path.is_file()]
        if segments:
            latest.add(max(segments, key=lambda path: path.stat().st_mtime_ns))
    return latest


def plan_retention(
    root: Path | str,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now_us: int | None = None,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> dict[str, object]:
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    now_us = now_us if now_us is not None else time.time_ns() // 1000
    cutoff_us = now_us - retention_days * DAY_US
    policy = disk_policy(metrics_root, disk_usage=disk_usage)
    backup = latest_verified_backup(metrics_root)
    segments = []
    raw_events = normalized_sessions = 0
    oldest_retained_us = None
    if db_path.exists():
        conn = connect(db_path, readonly=True)
        try:
            cursors = {
                str(row["segment_path"]): int(row["committed_offset"])
                for row in conn.execute(
                    "SELECT segment_path,committed_offset FROM spool_cursors"
                )
            }
            old_turns = _old_turns_sql()
            old_sessions = _old_sessions_sql()
            raw_events = int(conn.execute(
                "SELECT COUNT(*) FROM raw_events AS r "
                "WHERE r.projection_state='projected' AND r.received_at_us<? AND ("
                f"(r.turn_id IS NOT NULL AND r.turn_id IN ({old_turns})) OR "
                f"(r.turn_id IS NULL AND r.session_id IN ({old_sessions})))",
                (cutoff_us, cutoff_us, cutoff_us, cutoff_us, cutoff_us),
            ).fetchone()[0])
            normalized_sessions = int(conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE session_id IN ({old_sessions})",
                (cutoff_us, cutoff_us),
            ).fetchone()[0])
            row = conn.execute(
                "SELECT MIN(received_at_us) FROM raw_events"
            ).fetchone()
            oldest_retained_us = row[0] if row else None
        finally:
            conn.close()
        active = _latest_active_segments_by_producer(metrics_root / "spool")
        for segment in sorted((metrics_root / "spool").glob("*/*.telemetry")):
            stat = segment.stat()
            if segment in active or cursors.get(str(segment), -1) != stat.st_size:
                continue
            expired = stat.st_mtime_ns // 1000 < cutoff_us
            if expired or policy.pressure:
                segments.append({
                    "path": str(segment),
                    "bytes": stat.st_size,
                    "mtime_us": stat.st_mtime_ns // 1000,
                    "reason": "disk_pressure" if policy.pressure and not expired else "expired",
                })
    blocked = bool((segments or raw_events or normalized_sessions) and not backup)
    effective_from = cutoff_us
    if policy.pressure and segments:
        effective_from = max(item["mtime_us"] for item in segments)
    return {
        "status": "blocked" if blocked else "ready",
        "retention_days": retention_days,
        "cutoff_us": cutoff_us,
        "retained_from_us": oldest_retained_us,
        "effective_retained_from_us": effective_from,
        "retention_truncated": bool(policy.pressure),
        "retention_blocked": blocked,
        "backup": backup,
        "disk": policy.__dict__,
        "archive_segments": segments,
        "projected_raw_events_to_prune": raw_events,
        "normalized_sessions_to_prune": normalized_sessions,
        "estimated_bytes_reclaimed": sum(item["bytes"] for item in segments),
    }


def _archive_segment(metrics_root: Path, segment: Path) -> Path:
    date = datetime.fromtimestamp(
        segment.stat().st_mtime, tz=timezone.utc
    ).strftime("%Y-%m-%d")
    target = (
        metrics_root / "archive" / date / segment.parent.name
        / f"{segment.name}.gz"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.", dir=target.parent, delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    original_hash = hashlib.sha256(segment.read_bytes()).hexdigest()
    try:
        with segment.open("rb") as source, gzip.open(tmp_path, "wb") as compressed:
            shutil.copyfileobj(source, compressed)
        with gzip.open(tmp_path, "rb") as restored:
            restored_hash = hashlib.sha256(restored.read()).hexdigest()
        if restored_hash != original_hash:
            raise RuntimeError(f"archive verification failed for {segment}")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
    return target


def apply_retention(
    root: Path | str,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now_us: int | None = None,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    _lock_held: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    def execute() -> dict[str, object]:
        before = plan_retention(
            root,
            retention_days=retention_days,
            now_us=now_us,
            disk_usage=disk_usage,
        )
        if not any((
            before["archive_segments"],
            before["projected_raw_events_to_prune"],
            before["normalized_sessions_to_prune"],
        )):
            return {**before, "status": "done", "applied": False}
        backup = create_backup_locked(metrics_root)
        plan = plan_retention(
            root,
            retention_days=retention_days,
            now_us=now_us,
            disk_usage=disk_usage,
        )
        archived = []
        for item in plan["archive_segments"]:
            archived.append(str(_archive_segment(metrics_root, Path(item["path"]))))
        db_path = metrics_root / "metrics.db"
        conn = connect(db_path)
        cutoff_us = int(plan["cutoff_us"])
        try:
            conn.execute("BEGIN IMMEDIATE")
            old_turns = _old_turns_sql()
            old_sessions = _old_sessions_sql()
            conn.execute(
                "DELETE FROM raw_events WHERE event_id IN ("
                "SELECT r.event_id FROM raw_events AS r "
                "WHERE r.projection_state='projected' AND r.received_at_us<? AND ("
                f"(r.turn_id IS NOT NULL AND r.turn_id IN ({old_turns})) OR "
                f"(r.turn_id IS NULL AND r.session_id IN ({old_sessions}))))",
                (cutoff_us, cutoff_us, cutoff_us, cutoff_us, cutoff_us),
            )
            conn.execute(
                f"DELETE FROM experiment_outcomes WHERE router_decision_id IN ("
                f"SELECT router_decision_id FROM router_decisions WHERE turn_id IN ({old_turns}))",
                (cutoff_us, cutoff_us),
            )
            conn.execute(
                f"DELETE FROM cost_measurements WHERE turn_id IN ({old_turns}) OR "
                f"(scope='session' AND session_id IN ({old_sessions}))",
                (cutoff_us, cutoff_us, cutoff_us, cutoff_us),
            )
            for table in (
                "tool_executions",
                "usage_measurements",
                "llm_invocations",
                "router_decisions",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE turn_id IN ({old_turns})",
                    (cutoff_us, cutoff_us),
                )
            conn.execute(
                f"DELETE FROM workflow_steps WHERE turn_id IN ({old_turns}) OR "
                f"(turn_id IS NULL AND session_id IN ({old_sessions}))",
                (cutoff_us, cutoff_us, cutoff_us, cutoff_us),
            )
            conn.execute(
                f"DELETE FROM turns WHERE turn_id IN ({old_turns})",
                (cutoff_us, cutoff_us),
            )
            conn.execute(
                f"DELETE FROM sessions WHERE session_id IN ({old_sessions})",
                (cutoff_us, cutoff_us),
            )
            for item in plan["archive_segments"]:
                conn.execute(
                    "DELETE FROM spool_cursors WHERE segment_path=?",
                    (item["path"],),
                )
            conn.commit()
            if integrity_check(conn) != "ok":
                raise RuntimeError("metrics database failed integrity_check after prune")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        for item in plan["archive_segments"]:
            Path(item["path"]).unlink(missing_ok=True)
        return {
            **plan,
            "status": "done",
            "applied": True,
            "backup": backup,
            "archived_segments": archived,
        }
    if _lock_held:
        return execute()
    lock_path = metrics_root / "collector"
    with try_file_lock(lock_path) as acquired:
        if not acquired:
            raise RetentionBusy("metrics collector is busy")
        return execute()
