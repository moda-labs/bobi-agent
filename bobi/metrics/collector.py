"""Single-winner collector for durable metrics ingestion and projection."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json, try_file_lock
from bobi.metrics.projection import project_pending
from bobi.metrics.spool import SpoolCorruptionError, iter_frames
from bobi.metrics.store import (
    StageResult,
    connect,
    cursor_offset,
    migrate,
    stage_frames,
)

log = logging.getLogger(__name__)


_SEGMENT_OWNER = re.compile(r"(?:producer|reconciler)-(\d+)-")


def project_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Project every pending raw event, in batches."""
    projection = {"selected": 0, "projected": 0, "deferred": 0, "quarantined": 0}
    while True:
        batch = project_pending(conn)
        for key in projection:
            projection[key] += batch[key]
        if batch["selected"] == 0:
            break
    return {key: projection[key] for key in ("projected", "deferred", "quarantined")}


# A fully imported segment is retired only once it has been quiet this long.
# Producer liveness is a pid check, so every writer of a spool must share this
# process's pid namespace; the quiet period covers a writer between appends.
_RETIRE_AFTER_SECONDS = 5 * 60


def _retirable(segment: Path) -> bool:
    """True for a fully imported segment nothing will append to again.

    A reconciler writes and closes its segment within one call, so age alone
    decides. A producer must also have exited; a reused pid reads as alive,
    which only delays retirement.
    """
    match = _SEGMENT_OWNER.match(segment.parent.name)
    if not match:
        return False
    try:
        if time.time() - segment.stat().st_mtime < _RETIRE_AFTER_SECONDS:
            return False
    except OSError:
        return False
    if segment.parent.name.startswith("reconciler-"):
        return True
    try:
        os.kill(int(match.group(1)), 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _retire_segment(conn: sqlite3.Connection, segment: Path) -> dict[str, int]:
    """Delete a retirable segment, and its directory once no segment is left.

    Its events are already in ``raw_events``. Returns the drop and writer-error
    counts from the producer's health file when the directory goes, so the
    collector can keep them.
    """
    segment.unlink()
    conn.execute("DELETE FROM spool_cursors WHERE segment_path=?", (str(segment),))
    conn.commit()
    if any(segment.parent.glob("*.telemetry")):
        return {}
    counts: dict[str, int] = {}
    try:
        data = json.loads((segment.parent / "health.json").read_text())
        counts = {
            "telemetry_events_dropped": int(data.get("telemetry_events_dropped") or 0),
            "writer_errors": int(data.get("writer_errors") or 0),
        }
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    for leftover in segment.parent.iterdir():
        leftover.unlink()
    segment.parent.rmdir()
    return counts


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
            stage_error = None
            try:
                stage = self._stage_segment(
                    conn, Path(segment_path), cursor_offset(conn, segment_path)
                )
            except SpoolCorruptionError as exc:
                stage = StageResult(0, 0)
                stage_error = exc
            projection = project_all(conn)
            if stage_error is not None:
                raise stage_error
            return {
                "role": "active",
                "staged": stage.staged,
                "conflicts": stage.conflicts,
                **projection,
            }
        finally:
            conn.close()

    @staticmethod
    def _stage_segment(conn, segment_path: Path, start: int) -> StageResult:
        staged = conflicts = 0
        batch = []
        batch_started_ns = time.perf_counter_ns()

        def flush() -> None:
            nonlocal staged, conflicts
            result = stage_frames(conn, segment_path=segment_path, frames=batch)
            staged += result.staged
            conflicts += result.conflicts

        frames = iter(iter_frames(segment_path, start))
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except Exception:
                if batch:
                    flush()
                raise
            batch.append(frame)
            deadline_reached = (
                time.perf_counter_ns() - batch_started_ns >= 25_000_000
            )
            if len(batch) < 500 and not deadline_reached:
                continue
            flush()
            batch = []
            batch_started_ns = time.perf_counter_ns()
        if batch:
            flush()
        return StageResult(staged, conflicts)

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
        self._reconciliation_attempts: dict[str, int] = {}
        self._load_reconciliation_attempts()
        # Counters that must outlive one cycle and a restart.
        self._lasting = self._load_lasting_counters()
        # One warning per segment and error type; repeats go to debug.
        self._reported_failures: set[tuple[str, str]] = set()
        self._retired_since_publish = False
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
            "last_reconciliation_at_us": None,
            "reconciliation_errors": 0,
            "reconciliation_attempts": dict(self._reconciliation_attempts),
            "reconciliation_retry_exhausted_turns": 0,
            "uncovered_turns": 0,
            **self._lasting,
        }

    def _load_reconciliation_attempts(self) -> None:
        try:
            saved = json.loads(self.health_path.read_text()).get("reconciliation_attempts", {})
            if isinstance(saved, dict):
                self._reconciliation_attempts = {
                    turn_id: count for turn_id, count in saved.items()
                    if isinstance(turn_id, str) and type(count) is int and 0 < count <= 3
                }
        except (OSError, ValueError, AttributeError):
            pass

    _LASTING_COUNTERS = (
        "conflicting_events", "last_conflict_at_us",
        "retired_producer_events_dropped", "retired_producer_writer_errors",
    )

    def _load_lasting_counters(self) -> dict[str, int | None]:
        lasting: dict[str, int | None] = {
            "conflicting_events": 0, "last_conflict_at_us": None,
            "retired_producer_events_dropped": 0, "retired_producer_writer_errors": 0,
        }
        try:
            saved = json.loads(self.health_path.read_text())
        except (OSError, ValueError):
            return lasting
        if isinstance(saved, dict):
            for key in self._LASTING_COUNTERS:
                if type(saved.get(key)) is int and saved[key] >= 0:
                    lasting[key] = saved[key]
        return lasting

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
        """One cycle: one connection, one migrate, then only the segments with
        bytes past their cursor. A segment that fails, for any reason, is
        counted and skipped so it cannot stop the ones after it."""
        totals: dict[str, int | str] = {
            **self._empty_result("active"), "conflicts": 0, "failed_segments": 0,
        }
        conn = connect(self.db_path)
        try:
            migrate(conn)
            cursors = {
                str(row["segment_path"]): int(row["committed_offset"])
                for row in conn.execute(
                    "SELECT segment_path, committed_offset FROM spool_cursors")
            }
            for segment in sorted(self.spool_root.glob("*/*.telemetry")):
                try:
                    size = segment.stat().st_size
                except OSError:
                    continue
                start = cursors.get(str(segment), 0)
                if start >= size:
                    if _retirable(segment):
                        self._retire(conn, segment)
                    continue
                try:
                    stage = self.collector._stage_segment(conn, segment, start)
                except SpoolCorruptionError:
                    log.debug("metrics: corrupt spool segment isolated: %s",
                              segment, exc_info=True)
                    totals["corrupt_segments"] = int(totals["corrupt_segments"]) + 1
                    continue
                except Exception as exc:
                    self._report_failure(segment, exc)
                    totals["failed_segments"] = int(totals["failed_segments"]) + 1
                    totals["failure"] = type(exc).__name__
                    continue
                totals["staged"] = int(totals["staged"]) + stage.staged
                totals["conflicts"] = int(totals["conflicts"]) + stage.conflicts
                if stage.conflicts:
                    self._lasting["conflicting_events"] = (
                        int(self._lasting["conflicting_events"] or 0) + stage.conflicts)
                    self._lasting["last_conflict_at_us"] = time.time_ns() // 1000
            for key, value in project_all(conn).items():
                totals[key] = int(totals[key]) + value
        finally:
            conn.close()
        return totals

    def _take_over(self) -> None:
        """Become the active collector: state the previous one saved wins over
        whatever this instance read when it started on standby."""
        self._load_reconciliation_attempts()
        self._lasting = self._load_lasting_counters()
        self._set_health(role="active",
                         reconciliation_attempts=dict(self._reconciliation_attempts),
                         **self._lasting)

    def _report_failure(self, segment: Path, exc: Exception) -> None:
        key = (str(segment), type(exc).__name__)
        if key in self._reported_failures:
            log.debug("metrics: spool segment still failing: %s", segment, exc_info=True)
            return
        self._reported_failures.add(key)
        log.warning("metrics: spool segment skipped until it imports: %s",
                    segment, exc_info=True)

    def _retire(self, conn: sqlite3.Connection, segment: Path) -> None:
        try:
            counts = _retire_segment(conn, segment)
        except (OSError, sqlite3.Error):
            log.debug("metrics: could not retire spool segment %s", segment, exc_info=True)
            return
        self._lasting["retired_producer_events_dropped"] = int(
            self._lasting["retired_producer_events_dropped"] or 0
        ) + counts.get("telemetry_events_dropped", 0)
        self._lasting["retired_producer_writer_errors"] = int(
            self._lasting["retired_producer_writer_errors"] or 0
        ) + counts.get("writer_errors", 0)
        if counts:
            # The producer's own file is gone; these counts now live only here.
            self._retired_since_publish = True

    def _run(self) -> None:
        while not self._stop.is_set():
            with try_file_lock(self.collector.lock_path) as acquired:
                if not acquired:
                    self._refresh_health(self._empty_result("standby"))
                    self._stop.wait(self.poll_interval)
                    continue
                self._take_over()
                while not self._stop.is_set():
                    self._cycle_locked()
                    self._stop.wait(self.poll_interval)
                # Import anything producers flushed while shutdown propagated.
                self._cycle_locked()
                self._set_health(status="stopped")
                self._publish_health(force=True)
                break
        self._set_health(status="stopped")

    def _cycle_locked(self) -> None:
        try:
            result = self._collect_once_locked()
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
        from bobi.metrics.reconcile import reconcile_missing

        errors = int(self.health()["reconciliation_errors"])
        try:
            reconciliation = reconcile_missing(
                self.root, collect=False, terminal_only=True,
                retry_attempts=self._reconciliation_attempts,
            )
            errors += int(reconciliation["errors"])
            self._set_health(reconciliation_retry_exhausted_turns=reconciliation.get("retry_exhausted_turns", 0))
        except Exception:
            log.debug("metrics: scheduled reconciliation failed", exc_info=True)
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
            reconciliation_attempts=dict(self._reconciliation_attempts),
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
        failed_segments = int(result.get("failed_segments", 0))
        conflicts = int(result.get("conflicts", 0))
        drops += int(self._lasting["retired_producer_events_dropped"] or 0)
        writer_errors += int(self._lasting["retired_producer_writer_errors"] or 0)
        self._set_health(
            status=(
                "degraded"
                if corrupt_segments or failed_segments
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
            last_error=(
                "SpoolCorruptionError" if corrupt_segments
                else str(result.get("failure") or "SegmentImportError") if failed_segments
                else "ConflictingEvent" if conflicts else None
            ),
            rejected_events=corrupt_segments + failed_segments + conflicts,
            quarantined_events=quarantined,
            telemetry_events_dropped=drops,
            producer_writer_errors=writer_errors,
            producer_count=producers,
            **self._lasting,
        )
        # A conflict is rare and worth keeping: write it now, not on the
        # once-a-second schedule.
        self._publish_health(force=bool(conflicts) or self._retired_since_publish)
        self._retired_since_publish = False

    def _set_health(self, **updates: object) -> None:
        with self._health_lock:
            self._health.update(updates)

    def _publish_health(self, *, force: bool = False) -> None:
        if self.health()["role"] != "active":
            return
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
