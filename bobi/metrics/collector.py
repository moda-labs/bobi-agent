"""Single-winner prototype collector for Phase 0 verification."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from bobi.fsutil import try_file_lock
from bobi.metrics.projection import project_pending
from bobi.metrics.spool import SpoolCorruptionError, iter_frames
from bobi.metrics.store import (
    connect,
    copy_raw_events,
    cursor_offset,
    integrity_check,
    logical_snapshot,
    migrate,
    stage_frames,
)


class MetricsCollector:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_name("collector")

    def collect_segment(self, segment_path: Path | str) -> dict[str, int | str]:
        """Import and project one segment, or return standby without waiting."""
        with try_file_lock(self.lock_path) as acquired:
            if not acquired:
                return {"role": "standby", "staged": 0, "projected": 0}
            conn = connect(self.db_path)
            try:
                migrate(conn)
                start = cursor_offset(conn, segment_path)
                stage_error = None
                try:
                    staged = self._stage_segment(conn, Path(segment_path), start)
                except SpoolCorruptionError as exc:
                    staged = 0
                    stage_error = exc
                projection = {
                    "selected": 0,
                    "projected": 0,
                    "deferred": 0,
                    "quarantined": 0,
                }
                while True:
                    batch = project_pending(conn)
                    for key in projection:
                        projection[key] += batch[key]
                    if batch["selected"] == 0:
                        break
                if stage_error is not None:
                    raise stage_error
                return {
                    "role": "active",
                    "staged": staged,
                    "projected": projection["projected"],
                    "deferred": projection["deferred"],
                    "quarantined": projection["quarantined"],
                }
            finally:
                conn.close()

    @staticmethod
    def _stage_segment(conn, segment_path: Path, start: int) -> int:
        staged = 0
        batch = []
        batch_started_ns = time.perf_counter_ns()
        frames = iter(iter_frames(segment_path, start))
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except Exception:
                if batch:
                    staged += stage_frames(
                        conn, segment_path=segment_path, frames=batch
                    )
                raise
            batch.append(frame)
            deadline_reached = (
                time.perf_counter_ns() - batch_started_ns >= 25_000_000
            )
            if len(batch) < 500 and not deadline_reached:
                continue
            staged += stage_frames(
                conn, segment_path=segment_path, frames=batch
            )
            batch = []
            batch_started_ns = time.perf_counter_ns()
        if batch:
            staged += stage_frames(conn, segment_path=segment_path, frames=batch)
        return staged


def rebuild_database(
    source_path: Path | str,
    target_path: Path | str,
) -> dict[str, object]:
    """Recreate normalized projections from retained immutable raw events."""
    source_path = Path(source_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        source = connect(source_path, readonly=True)
        target = connect(tmp_path)
        try:
            migrate(target)
            copy_raw_events(source, target)
            while True:
                result = project_pending(target, limit=500)
                if result["selected"] == 0:
                    break
            if integrity_check(target) != "ok":
                raise RuntimeError("rebuilt metrics database failed integrity_check")
            snapshot = logical_snapshot(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            source.close()
            target.close()
        tmp_path.replace(target_path)
        return snapshot
    finally:
        tmp_path.unlink(missing_ok=True)
