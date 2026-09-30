"""Single-winner prototype collector for Phase 0 verification."""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json, try_file_lock
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

log = logging.getLogger(__name__)


class MetricsCollector:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_name("collector")

    def collect_segment(self, segment_path: Path | str) -> dict[str, int | str]:
        """Import and project one segment, or return standby without waiting."""
        with try_file_lock(self.lock_path) as acquired:
            if not acquired:
                return {"role": "standby", "staged": 0, "projected": 0}
            return self.collect_segment_locked(segment_path)

    def collect_segment_locked(
        self, segment_path: Path | str
    ) -> dict[str, int | str]:
        """Import one segment while the caller holds the collector lock."""
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

    def prepare(self) -> str:
        """Create/migrate the read model under the non-blocking collector lock."""
        with try_file_lock(self.lock_path) as acquired:
            if not acquired:
                return "standby"
            self.prepare_locked()
            return "active"

    def prepare_locked(self) -> None:
        conn = connect(self.db_path)
        try:
            migrate(conn)
        finally:
            conn.close()


class MetricsCollectorService:
    """Supervisor-owned background importer with a bounded shutdown drain."""

    def __init__(
        self,
        root: Path | str,
        *,
        poll_interval: float = 0.1,
        reconcile_interval: float = 60.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.metrics_root = self.root / "state" / "metrics"
        self.spool_root = self.metrics_root / "spool"
        self.db_path = self.metrics_root / "metrics.db"
        self.health_path = self.metrics_root / "collector.state.json"
        self.collector = MetricsCollector(self.db_path)
        self.poll_interval = poll_interval
        self.reconcile_interval = reconcile_interval
        self._last_reconcile = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="bobi-metrics-collector",
        )
        self._started = False
        self._health_lock = threading.Lock()
        self._last_health_write = 0.0
        self._health: dict[str, object] = {
            "status": "starting",
            "role": "standby",
            "db_ready": False,
            "spool_bytes": 0,
            "uncommitted_spool_bytes": 0,
            "import_lag_ms": None,
            "last_success_at_us": None,
            "last_error": None,
            "rejected_events": 0,
            "quarantined_events": 0,
            "telemetry_events_dropped": 0,
            "producer_writer_errors": 0,
            "producer_count": 0,
            "maintenance_queue_depth": 0,
            "last_maintenance": None,
            "last_reconciliation_at_us": None,
            "reconciliation_errors": 0,
            "uncovered_turns": 0,
        }

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self, *, timeout: float = 5.0) -> bool:
        if not self._started:
            return True
        self._stop.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def health(self) -> dict[str, object]:
        with self._health_lock:
            return dict(self._health)

    def collect_once(self) -> dict[str, int | str]:
        with try_file_lock(self.collector.lock_path) as acquired:
            if not acquired:
                return self._empty_result("standby")
            return self._collect_once_locked()

    @staticmethod
    def _empty_result(role: str) -> dict[str, int | str]:
        return {
            "role": role,
            "staged": 0,
            "projected": 0,
            "deferred": 0,
            "quarantined": 0,
            "corrupt_segments": 0,
        }

    def _collect_once_locked(self) -> dict[str, int | str]:
        totals: dict[str, int | str] = {
            "role": "active",
            "staged": 0,
            "projected": 0,
            "deferred": 0,
            "quarantined": 0,
            "corrupt_segments": 0,
        }
        self.collector.prepare_locked()
        for segment in sorted(self.spool_root.glob("*/*.telemetry")):
            try:
                result = self.collector.collect_segment_locked(segment)
            except SpoolCorruptionError:
                log.debug(
                    "metrics: corrupt spool segment isolated: %s",
                    segment,
                    exc_info=True,
                )
                totals["corrupt_segments"] = (
                    int(totals["corrupt_segments"]) + 1
                )
                continue
            for key in ("staged", "projected", "deferred", "quarantined"):
                totals[key] = int(totals[key]) + int(result[key])
        return totals

    def _run(self) -> None:
        while not self._stop.is_set():
            with try_file_lock(self.collector.lock_path) as acquired:
                if not acquired:
                    self._refresh_health(self._empty_result("standby"))
                    self._stop.wait(self.poll_interval)
                    continue
                while not self._stop.is_set():
                    self._cycle_locked()
                    self._stop.wait(self.poll_interval)
                # Import anything producers flushed while shutdown propagated.
                self._cycle_locked()
                break
        self._set_health(status="stopped")
        self._publish_health(force=True)

    def _cycle_locked(self) -> None:
        try:
            result = self._collect_once_locked()
            from bobi.metrics.maintenance import process_pending_requests

            maintenance = process_pending_requests(self.root)
            if maintenance:
                result = self._collect_once_locked()
                self._set_health(last_maintenance=maintenance[-1])
            if time.monotonic() - self._last_reconcile >= self.reconcile_interval:
                result = self._reconcile_locked(result)
            self._refresh_health(result)
        except Exception as exc:
            log.debug("metrics: collector cycle failed", exc_info=True)
            self._set_health(
                status="degraded",
                last_error=type(exc).__name__,
                rejected_events=int(self.health()["rejected_events"]) + 1,
            )
            self._publish_health()

    def _reconcile_locked(
        self, result: dict[str, int | str]
    ) -> dict[str, int | str]:
        from bobi.metrics.estimate import estimate_missing
        from bobi.metrics.reconcile import reconcile_missing

        errors = int(self.health()["reconciliation_errors"])
        try:
            reconciliation = reconcile_missing(
                self.root, collect=False, terminal_only=True
            )
            errors += int(reconciliation["errors"])
        except Exception:
            log.debug("metrics: scheduled reconciliation failed", exc_info=True)
            errors += 1
        result = self._collect_once_locked()
        try:
            estimate_missing(self.root, collect=False, terminal_only=True)
        except Exception:
            log.debug("metrics: scheduled estimation failed", exc_info=True)
            errors += 1
        result = self._collect_once_locked()
        uncovered = 0
        if self.db_path.exists():
            conn = connect(self.db_path, readonly=True)
            try:
                uncovered = int(conn.execute(
                    "SELECT COUNT(*) FROM turns AS t WHERE NOT EXISTS ("
                    "SELECT 1 FROM usage_measurements AS u WHERE u.turn_id=t.turn_id "
                    "AND u.scope='turn') AND t.ended_at_us IS NOT NULL"
                ).fetchone()[0])
            finally:
                conn.close()
        self._last_reconcile = time.monotonic()
        self._set_health(
            last_reconciliation_at_us=time.time_ns() // 1000,
            reconciliation_errors=errors,
            uncovered_turns=uncovered,
        )
        return result

    def _refresh_health(self, result: dict[str, int | str]) -> None:
        now_us = time.time_ns() // 1000
        segments = list(self.spool_root.glob("*/*.telemetry"))
        spool_bytes = 0
        uncommitted_spool_bytes = 0
        oldest_uncommitted_ns: int | None = None
        cursors: dict[str, int] = {}
        if self.db_path.exists():
            conn = connect(self.db_path, readonly=True)
            try:
                try:
                    cursors = {
                        str(row["segment_path"]): int(row["committed_offset"])
                        for row in conn.execute(
                            "SELECT segment_path, committed_offset FROM spool_cursors"
                        )
                    }
                except sqlite3.OperationalError:
                    cursors = {}
            finally:
                conn.close()
        for segment in segments:
            try:
                stat = segment.stat()
            except OSError:
                continue
            spool_bytes += stat.st_size
            pending = max(0, stat.st_size - cursors.get(str(segment), 0))
            if pending:
                uncommitted_spool_bytes += pending
                oldest_uncommitted_ns = (
                    stat.st_mtime_ns
                    if oldest_uncommitted_ns is None
                    else min(oldest_uncommitted_ns, stat.st_mtime_ns)
                )

        drops = writer_errors = producers = 0
        for health_path in self.spool_root.glob("*/health.json"):
            try:
                data = json.loads(health_path.read_text())
            except (OSError, ValueError, TypeError):
                continue
            producers += 1
            drops += int(data.get("telemetry_events_dropped") or 0)
            writer_errors += int(data.get("writer_errors") or 0)

        quarantined = 0
        if self.db_path.exists():
            conn = connect(self.db_path, readonly=True)
            try:
                try:
                    quarantined = int(conn.execute(
                        "SELECT COUNT(*) FROM raw_events "
                        "WHERE projection_state='quarantined'"
                    ).fetchone()[0])
                except sqlite3.OperationalError:
                    quarantined = 0
            finally:
                conn.close()
        corrupt_segments = int(result.get("corrupt_segments", 0))
        self._set_health(
            status=(
                "degraded"
                if corrupt_segments
                else "running" if result["role"] == "active" else "standby"
            ),
            role=result["role"],
            db_ready=self.db_path.exists(),
            spool_bytes=spool_bytes,
            uncommitted_spool_bytes=uncommitted_spool_bytes,
            import_lag_ms=(
                max(0.0, (time.time_ns() - oldest_uncommitted_ns) / 1_000_000)
                if oldest_uncommitted_ns is not None else 0.0
            ),
            last_success_at_us=now_us,
            last_error="SpoolCorruptionError" if corrupt_segments else None,
            rejected_events=corrupt_segments,
            quarantined_events=quarantined,
            telemetry_events_dropped=drops,
            producer_writer_errors=writer_errors,
            producer_count=producers,
            maintenance_queue_depth=len(list(
                (self.metrics_root / "maintenance").glob("*.request.json")
            )),
        )
        self._publish_health()

    def _set_health(self, **updates: object) -> None:
        with self._health_lock:
            self._health.update(updates)

    def _publish_health(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_health_write < 1.0:
            return
        try:
            atomic_write_json(
                self.health_path,
                self.health(),
                indent=None,
                sort_keys=True,
            )
            self._last_health_write = now
        except Exception:
            log.debug("metrics: collector health write failed", exc_info=True)


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
