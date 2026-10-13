"""Durable ingestion, SQLite integrity, and recovery regressions."""

import json
import os
import pytest
import sqlite3
import threading
import time
import uuid
from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.fsutil import try_file_lock
from bobi.metrics.collector import MetricsCollector, MetricsCollectorService
from bobi.metrics.events import MetricsEvent
from bobi.metrics.producer import MetricsProducer
from bobi.metrics.projection import ORPHAN_TTL_US, project_pending
from bobi.metrics.providers import (
    ProviderContractError,
    parse_claude_transcript_records,
    parse_codex_rollout_records,
)
from bobi.metrics.reconcile import (
    ReconciliationError,
    _aggregate,
    reconcile_missing,
    reconcile_turn,
)
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.spool import (
    HEADER,
    SpoolCorruptionError,
    SpoolWriter,
    encode_frame,
    iter_frames,
)
from bobi.metrics.store import (
    connect,
    cursor_offset,
    fill_model_keys,
    integrity_check,
    migrate,
    stage_frames,
)
from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import Supervisor
from dataclasses import replace
from datetime import datetime, timezone
from tests.metrics.helpers import historical_assignment, metrics_fixtures


def _producer_event(sequence=1):
    return MetricsEvent(
        event_type="session.recorded",
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload={"session_id": "session"},
    )


def test_producer_writes_on_background_thread(tmp_path):
    segment = tmp_path / "segment.telemetry"
    producer = MetricsProducer(segment)
    producer.start()
    assert producer.try_emit(_producer_event()) is True
    assert producer.close(timeout=2) is True
    assert [record.event.producer_sequence for record in iter_frames(segment)] == [1]


def test_producer_rejects_events_before_start_and_after_close(tmp_path):
    producer = MetricsProducer(tmp_path / "segment.telemetry")
    assert producer.try_emit(_producer_event(1)) is False
    assert producer.close() is True
    assert producer.try_emit(_producer_event(2)) is False
    assert producer.telemetry_events_dropped == 2


def test_producer_flushes_low_volume_queue_while_idle(monkeypatch, tmp_path):
    flushed = threading.Event()

    def flush_if_due(_writer):
        flushed.set()
        return True

    monkeypatch.setattr("bobi.metrics.spool.SpoolWriter.flush_if_due", flush_if_due)
    producer = MetricsProducer(tmp_path / "segment.telemetry")
    producer.start()
    assert producer.try_emit(_producer_event()) is True
    assert flushed.wait(1) is True
    assert producer.close(timeout=2) is True


def test_producer_contains_spool_failures_after_startup(tmp_path):
    class BrokenWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            raise OSError("disk unavailable")

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry", writer_factory=BrokenWriter
    )
    producer.start()

    assert producer.try_emit(_producer_event()) is True
    deadline = time.monotonic() + 1
    while producer.writer_errors == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert producer.try_emit(_producer_event(2)) is False
    assert producer.close(timeout=2) is True
    assert producer.writer_errors == 1
    assert producer.health()["writer_available"] is False


def test_producer_shutdown_is_bounded_when_writer_is_stuck(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            entered.set()
            release.wait(2)

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry", writer_factory=BlockingWriter
    )
    producer.start()
    assert producer.try_emit(_producer_event()) is True
    assert entered.wait(1) is True

    started = time.monotonic()
    assert producer.close(timeout=0.01) is False
    assert time.monotonic() - started < 0.25

    release.set()
    assert producer.close(timeout=1) is True


def test_queue_overflow_drops_without_blocking(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            entered.set()
            release.wait(2)

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry",
        capacity=1,
        writer_factory=BlockingWriter,
    )
    producer.start()
    assert producer.try_emit(_producer_event(1)) is True
    assert entered.wait(1) is True
    assert producer.try_emit(_producer_event(2)) is True

    started = time.perf_counter_ns()
    assert producer.try_emit(_producer_event(3)) is False
    assert (time.perf_counter_ns() - started) / 1000 < 200_000
    assert producer.telemetry_events_dropped == 1

    release.set()
    assert producer.close(timeout=1) is True


def event(sequence=1):
    return MetricsEvent(
        event_type="session.recorded",
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload={"session_id": "session"},
    )


def test_frame_round_trip_and_offsets(tmp_path):
    path = tmp_path / "segment.telemetry"
    with SpoolWriter(path) as writer:
        first_offset = writer.append(event(1))
        second_offset = writer.append(event(2))
    records = list(iter_frames(path))
    assert first_offset == 0
    assert second_offset == records[0].next_offset
    assert [record.event.producer_sequence for record in records] == [1, 2]
    assert uuid.UUID(records[0].event.event_id.removeprefix("evt_")).version == 7


def test_event_validation_remains_enabled_by_default():
    with pytest.raises(TypeError):
        MetricsEvent(
            event_type="session.recorded",
            producer_id="producer",
            producer_sequence=1,
            source="test",
            payload={"not_json": object()},
        )


def test_incomplete_tail_preserves_prior_frames(tmp_path):
    path = tmp_path / "segment.telemetry"
    complete = encode_frame(event(1))
    path.write_bytes(complete + encode_frame(event(2))[:-7])
    records = list(iter_frames(path))
    assert len(records) == 1
    assert records[0].next_offset == len(complete)


def test_checksum_corruption_is_rejected(tmp_path):
    path = tmp_path / "segment.telemetry"
    frame = bytearray(encode_frame(event()))
    frame[HEADER.size] ^= 1
    path.write_bytes(frame)
    with pytest.raises(SpoolCorruptionError, match="checksum"):
        list(iter_frames(path))


def test_schema_mismatch_is_rejected(tmp_path):
    path = tmp_path / "segment.telemetry"
    frame = bytearray(encode_frame(event()))
    frame[5] = 2
    path.write_bytes(frame)
    with pytest.raises(SpoolCorruptionError):
        list(iter_frames(path))


def test_writer_completes_short_os_writes(monkeypatch, tmp_path):
    path = tmp_path / "segment.telemetry"
    real_write = __import__("os").write
    calls = 0

    def short_write(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = payload[: max(1, len(payload) // 2)]
        return real_write(fd, payload)

    monkeypatch.setattr("bobi.metrics.spool.os.write", short_write)
    with SpoolWriter(path) as writer:
        writer.append(event())

    assert calls >= 2
    assert [record.event.producer_sequence for record in iter_frames(path)] == [1]


def test_writer_stops_after_partial_append_failure(monkeypatch, tmp_path):
    path = tmp_path / "segment.telemetry"
    with SpoolWriter(path) as writer:
        writer.append(event(1))
        real_write = __import__("os").write
        calls = 0

        def failing_write(fd, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, payload[: max(1, len(payload) // 2)])
            raise OSError("injected write failure")

        monkeypatch.setattr("bobi.metrics.spool.os.write", failing_write)
        with pytest.raises(OSError, match="injected"):
            writer.append(event(2))
        with pytest.raises(OSError, match="prior append failure"):
            writer.append(event(3))

    assert [record.event.producer_sequence for record in iter_frames(path)] == [1]


def test_writer_flushes_when_time_threshold_is_due(monkeypatch, tmp_path):
    path = tmp_path / "segment.telemetry"
    syncs = []
    monkeypatch.setattr("bobi.metrics.spool._sync", lambda fd: syncs.append(fd))
    with SpoolWriter(path, sync_interval_seconds=60, sync_bytes=10**9) as writer:
        writer.append(event())
        assert syncs == []
        writer._last_sync -= 61
        assert writer.flush_if_due() is True
        assert len(syncs) == 1
        assert writer.flush_if_due() is False


def test_service_imports_spools_and_exposes_health(tmp_path):
    service = MetricsCollectorService(tmp_path, poll_interval=0.01)
    service.start()
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn("agent", provider="openai", brain="codex")
    observation.record_result(TurnResult(
        session_id="thread-1",
        usage=[BrainUsage(
            model="gpt-test",
            provider_event_id="turn-1",
            input_tokens=10,
            output_tokens=2,
            raw_usage={"input_tokens": 10, "output_tokens": 2},
        )],
    ))
    observation.finish(status="completed")
    runtime.close()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if service.db_path.exists():
            conn = connect(service.db_path, readonly=True)
            try:
                try:
                    health = service.health()
                    if (conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
                            and health["status"] == "running"
                            and health["producer_count"] == 1
                            and health["uncommitted_spool_bytes"] == 0):
                        break
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        time.sleep(0.01)
    else:
        raise AssertionError("collector did not project the turn and publish ready health")

    health = service.health()
    assert health["status"] == "running"
    assert health["role"] == "active"
    assert health["db_ready"] is True
    assert health["producer_count"] == 1
    assert health["telemetry_events_dropped"] == 0
    assert health["uncommitted_spool_bytes"] == 0
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not service.health_path.exists():
        time.sleep(0.01)
    persisted = json.loads(service.health_path.read_text())
    assert persisted["role"] == "active"
    assert persisted["uncommitted_spool_bytes"] == 0
    assert service.stop(timeout=2.0)


def test_service_stays_standby_when_another_collector_holds_the_lock(tmp_path):
    from bobi.fsutil import try_file_lock

    service = MetricsCollectorService(tmp_path)
    with try_file_lock(service.collector.lock_path) as acquired:
        assert acquired
        result = service.collect_once()
    assert result["role"] == "standby"


def test_only_one_background_service_owns_the_collector_lock(tmp_path):
    first = MetricsCollectorService(tmp_path, poll_interval=0.01)
    second = MetricsCollectorService(tmp_path, poll_interval=0.01)
    first.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and first.health()["role"] != "active":
        time.sleep(0.01)
    second.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and second.health()["status"] == "starting":
        time.sleep(0.01)

    assert first.health()["role"] == "active"
    assert second.health()["role"] == "standby"
    assert first.stop(timeout=2)
    assert second.stop(timeout=2)


def test_collector_failure_is_health_only(monkeypatch, tmp_path):
    service = MetricsCollectorService(tmp_path)
    monkeypatch.setattr(
        service, "_collect_once_locked", lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        )
    )

    service._cycle_locked()

    assert service.health()["status"] == "degraded"
    assert service.health()["last_error"] == "OperationalError"
    assert service.health()["rejected_events"] == 1


def test_collector_does_not_reconcile_immediately_on_startup(monkeypatch, tmp_path):
    service = MetricsCollectorService(tmp_path, reconcile_interval=60)
    calls = []
    monkeypatch.setattr(
        service, "_reconcile_locked", lambda result: calls.append(result) or result
    )

    service._cycle_locked()

    assert calls == []
    assert service.health()["last_reconciliation_at_us"] is None


def test_collector_reconciles_after_interval(monkeypatch, tmp_path):
    service = MetricsCollectorService(tmp_path, reconcile_interval=60)
    service._last_reconcile = time.monotonic() - 61
    calls = []

    def reconcile(result):
        calls.append(result)
        service._set_health(last_reconciliation_at_us=123)
        return result

    monkeypatch.setattr(service, "_reconcile_locked", reconcile)

    service._cycle_locked()

    assert len(calls) == 1
    assert service.health()["last_reconciliation_at_us"] == 123


def test_scheduled_reconciliation_collects_exact_usage(monkeypatch, tmp_path):
    import bobi.metrics.reconcile as reconcile_module

    service = MetricsCollectorService(tmp_path)
    order = []
    monkeypatch.setattr(
        reconcile_module,
        "reconcile_missing",
        lambda root, collect, terminal_only, track_attempts, budget_seconds: (
            order.append(("reconcile", terminal_only)) or {"errors": 0}
        ),
    )
    monkeypatch.setattr(
        service,
        "_collect_once_locked",
        lambda: order.append("collect") or service._empty_result("active"),
    )

    service._reconcile_locked(service._empty_result("active"))

    assert order == [("reconcile", True), "collect"]
    assert service.health()["reconciliation_errors"] == 0
    assert service.health()["last_reconciliation_at_us"] is not None


def test_scheduled_reconciliation_failures_are_health_only(monkeypatch, tmp_path):
    import bobi.metrics.reconcile as reconcile_module

    service = MetricsCollectorService(tmp_path)
    service._set_health(reconciliation_errors=4)
    monkeypatch.setattr(
        reconcile_module,
        "reconcile_missing",
        lambda root, collect, terminal_only, track_attempts, budget_seconds: (
            _ for _ in ()
        ).throw(RuntimeError("reconcile")),
    )
    monkeypatch.setattr(
        service, "_collect_once_locked", lambda: service._empty_result("active")
    )

    result = service._reconcile_locked(service._empty_result("active"))

    assert result["role"] == "active"
    assert service.health()["reconciliation_errors"] == 5
    assert service.health()["last_reconciliation_at_us"] is not None


def test_reconciliation_health_is_persisted(monkeypatch, tmp_path):
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)

    def reconcile(result):
        service._set_health(
            last_reconciliation_at_us=123,
            reconciliation_errors=2,
            uncovered_turns=3,
        )
        return result

    monkeypatch.setattr(service, "_reconcile_locked", reconcile)

    service._cycle_locked()

    persisted = json.loads(service.health_path.read_text())
    assert persisted["last_reconciliation_at_us"] == 123
    assert persisted["reconciliation_errors"] == 2
    assert persisted["uncovered_turns"] == 3


def test_corrupt_segment_does_not_starve_later_segments(tmp_path):
    service = MetricsCollectorService(tmp_path)
    corrupt = service.spool_root / "a" / "one.telemetry"
    valid = service.spool_root / "b" / "two.telemetry"
    event = MetricsEvent(
        event_type="session.recorded",
        producer_id="fixture-corrupt",
        producer_sequence=1,
        source="test",
        session_id="session-valid",
        payload={
            "session_id": "session-valid",
            "session_name": "agent",
            "brain": "stub",
            "provider": "stub",
            "started_at_us": 1,
            "status": "completed",
        },
    )
    with SpoolWriter(corrupt) as writer:
        offset = writer.append(event)
    data = bytearray(corrupt.read_bytes())
    data[offset + HEADER.size] ^= 1
    corrupt.write_bytes(data)
    with SpoolWriter(valid) as writer:
        writer.append(MetricsEvent(
            event_type=event.event_type,
            producer_id="fixture-valid",
            producer_sequence=1,
            source=event.source,
            session_id=event.session_id,
            payload=event.payload,
        ))

    result = service.collect_once()

    assert result["corrupt_segments"] == 1
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()


def _session_event(producer_id, sequence, session_id, *, status="completed", event_id=None):
    payload = {"session_id": session_id, "session_name": "agent", "brain": "stub",
               "provider": "stub", "started_at_us": 1, "status": status}
    kwargs = {"event_id": event_id} if event_id else {}
    return MetricsEvent(event_type="session.recorded", producer_id=producer_id,
                        producer_sequence=sequence, source="test",
                        session_id=session_id, payload=payload, **kwargs)


def test_a_conflicting_frame_does_not_stop_later_frames_or_segments(tmp_path):
    service = MetricsCollectorService(tmp_path)
    original = _session_event("producer-1-a", 1, "s1")
    with SpoolWriter(service.spool_root / "producer-1-a" / "1.telemetry") as writer:
        writer.append(original)
    assert service.collect_once()["staged"] == 1
    with SpoolWriter(service.spool_root / "reconciler-1-b" / "2.telemetry") as writer:
        writer.append(_session_event("reconciler-1-b", 1, "s1", status="failed",
                                     event_id=original.event_id))
        writer.append(_session_event("reconciler-1-b", 2, "s2"))
    with SpoolWriter(service.spool_root / "reconciler-1-c" / "3.telemetry") as writer:
        writer.append(_session_event("reconciler-1-c", 1, "s3"))

    result = service.collect_once()
    service._refresh_health(result)

    assert result["conflicts"] == 1
    assert result["staged"] == 2
    conn = connect(service.db_path, readonly=True)
    try:
        assert {row[0]: row[1] for row in conn.execute(
            "SELECT session_id, status FROM sessions")} == {
            "s1": "completed", "s2": "completed", "s3": "completed"}
    finally:
        conn.close()
    assert service.health()["status"] != "degraded"


def test_a_segment_that_fails_for_any_reason_does_not_stop_the_others(tmp_path, monkeypatch):
    from bobi.metrics import collector as collector_module

    service = MetricsCollectorService(tmp_path)
    for name in ("a", "b"):
        with SpoolWriter(service.spool_root / f"producer-1-{name}" / "1.telemetry") as writer:
            writer.append(_session_event(f"producer-1-{name}", 1, f"s-{name}"))
    real = collector_module.iter_frames

    def failing(path, start=0):
        if "producer-1-a" in str(path):
            raise RuntimeError("unexpected")
        return real(path, start)

    monkeypatch.setattr(collector_module, "iter_frames", failing)
    result = service.collect_once()

    assert result["failed_segments"] == 1
    assert result["staged"] == 1


def test_a_cycle_opens_one_connection_and_skips_consumed_segments(tmp_path, monkeypatch):
    from bobi.metrics import collector as collector_module

    service = MetricsCollectorService(tmp_path)
    for index in range(5):
        with SpoolWriter(service.spool_root / f"producer-{os.getpid()}-{index}" / "1.telemetry") as writer:
            writer.append(_session_event("p", index, f"s{index}"))
    service.collect_once()
    calls = {"connect": 0, "migrate": 0, "iter_frames": 0}
    for name in calls:
        real = getattr(collector_module, name)

        def counted(*args, _real=real, _name=name, **kwargs):
            calls[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(collector_module, name, counted)

    service.collect_once()

    assert calls == {"connect": 1, "migrate": 1, "iter_frames": 0}


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_quiet_imported_segments_of_exited_writers_are_retired(tmp_path):
    import subprocess
    import sys

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    service = MetricsCollectorService(tmp_path)
    gone = service.spool_root / f"producer-{dead}-x" / "1.telemetry"
    fresh = service.spool_root / f"producer-{dead}-y" / "1.telemetry"
    alive = service.spool_root / f"producer-{os.getpid()}-z" / "1.telemetry"
    reconciler = service.spool_root / f"reconciler-{os.getpid()}-r" / "1.telemetry"
    for index, path in enumerate((gone, fresh, alive, reconciler)):
        with SpoolWriter(path) as writer:
            writer.append(_session_event("p", index, f"s{index}"))
    (gone.parent / "health.json").write_text(json.dumps(
        {"telemetry_events_dropped": 3, "writer_errors": 1}))
    service.collect_once()  # imports all four
    for path in (gone, alive, reconciler):
        _age(path, 10 * 60)

    result = service.collect_once()
    service._refresh_health(result)

    assert not gone.parent.exists()
    assert not reconciler.parent.exists()
    assert fresh.exists() and alive.exists()
    health = service.health()
    assert health["telemetry_events_dropped"] == 3
    assert health["producer_writer_errors"] == 1
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 4
        assert sorted(row[0] for row in conn.execute("SELECT segment_path FROM spool_cursors")) == \
            sorted([str(fresh), str(alive)])
    finally:
        conn.close()


def test_a_dead_writers_torn_tail_is_counted_and_retired(tmp_path):
    import subprocess
    import sys

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    service = MetricsCollectorService(tmp_path)
    torn = service.spool_root / f"producer-{dead}-x" / "1.telemetry"
    with SpoolWriter(torn) as writer:
        writer.append(_session_event("p", 1, "s1"))
    whole = torn.read_bytes()
    with torn.open("ab") as handle:
        handle.write(whole[: len(whole) // 2])  # a short write, then the writer died
    _age(torn, 60 * 60)

    result = service.collect_once()
    service._refresh_health(result)

    assert not torn.parent.exists()
    health = service.health()
    assert health["torn_tail_bytes"] == len(whole) // 2
    assert health["import_lag_ms"] == 0.0
    assert health["uncommitted_spool_bytes"] == 0


def test_a_conflict_stays_in_collector_health(tmp_path):
    service = MetricsCollectorService(tmp_path)
    original = _session_event("producer-1-a", 1, "s1")
    with SpoolWriter(service.spool_root / "producer-1-a" / "1.telemetry") as writer:
        writer.append(original)
    service._cycle_locked()
    with SpoolWriter(service.spool_root / "reconciler-1-b" / "2.telemetry") as writer:
        writer.append(_session_event("reconciler-1-b", 1, "s1", status="failed",
                                     event_id=original.event_id))
    service._cycle_locked()
    service._cycle_locked()

    assert service.health()["conflicting_events"] == 1
    saved = json.loads(service.health_path.read_text())
    assert saved["conflicting_events"] == 1
    assert saved["last_conflict_at_us"]
    assert MetricsCollectorService(tmp_path).health()["conflicting_events"] == 1


def test_a_collector_taking_over_keeps_the_saved_counters(tmp_path):
    standby = MetricsCollectorService(tmp_path)  # started before anything was saved
    active = MetricsCollectorService(tmp_path)
    original = _session_event("producer-1-a", 1, "s1")
    with SpoolWriter(active.spool_root / "producer-1-a" / "1.telemetry") as writer:
        writer.append(original)
    active._cycle_locked()
    with SpoolWriter(active.spool_root / "reconciler-1-b" / "2.telemetry") as writer:
        writer.append(_session_event("reconciler-1-b", 1, "s1", status="failed",
                                     event_id=original.event_id))
    active._cycle_locked()

    standby._take_over()
    standby._cycle_locked()
    standby._publish_health(force=True)

    assert json.loads(standby.health_path.read_text())["conflicting_events"] == 1


def test_a_segment_that_keeps_failing_is_reported_once(tmp_path, monkeypatch, caplog):
    import logging

    from bobi.metrics import collector as collector_module

    service = MetricsCollectorService(tmp_path)
    with SpoolWriter(service.spool_root / "producer-1-a" / "1.telemetry") as writer:
        writer.append(_session_event("producer-1-a", 1, "s1"))

    def failing(path, start=0):
        raise sqlite3.IntegrityError("drifted")

    monkeypatch.setattr(collector_module, "iter_frames", failing)
    with caplog.at_level(logging.WARNING, logger="bobi.metrics.collector"):
        for _ in range(3):
            service._cycle_locked()

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert service.health()["last_error"] == "IntegrityError"
    assert service.health()["status"] == "degraded"


def _store_event(event_type, sequence, payload, **ids):
    return MetricsEvent(
        event_type=event_type,
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload=payload,
        **ids,
    )


def _write_complete_segment(path):
    events = [
        _store_event(
            "session.recorded",
            1,
            {
                "session_id": "s1",
                "session_name": "agent",
                "brain": "claude",
                "provider": "anthropic",
                "started_at_us": 1,
                "status": "running",
            },
            session_id="s1",
        ),
        _store_event(
            "turn.recorded",
            2,
            {
                "turn_id": "t1",
                "session_id": "s1",
                "turn_index": 1,
                "trigger_kind": "user",
                "is_user_initiated": 1,
                "started_at_us": 2,
                "status": "completed",
            },
            session_id="s1",
            turn_id="t1",
        ),
        _store_event(
            "usage.recorded",
            3,
            {
                "measurement_id": "u1",
                "scope": "turn",
                "turn_id": "t1",
                "invocation_id": None,
                "provider": "anthropic",
                "model": "claude-test",
                "provider_event_id": "req-1",
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "token_semantics_version": 1,
                "input_tokens": 10,
                "cache_read_input_tokens": 2,
                "output_tokens": 3,
                "observed_at_us": 3,
            },
            session_id="s1",
            turn_id="t1",
        ),
    ]
    with SpoolWriter(path) as writer:
        for item in events:
            writer.append(item)


def test_collector_replay_is_idempotent(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    _write_complete_segment(segment)
    collector = MetricsCollector(db)
    first = collector.collect_segment(segment)
    second = collector.collect_segment(segment)
    assert first["staged"] == 3
    assert first["projected"] == 3
    assert second["staged"] == 0
    assert second["projected"] == 0
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM best_usage").fetchone()[0] == 1
    assert integrity_check(conn) == "ok"
    conn.close()


def test_collector_election_is_non_blocking(tmp_path):
    lock = tmp_path / "collector"
    with try_file_lock(lock) as acquired:
        assert acquired is True
        with try_file_lock(lock) as contender:
            assert contender is False


def test_schema_has_wal_foreign_keys_and_best_usage(tmp_path):
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert "raw_events" in names
    assert "best_usage" in names
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_cost_invocation" in indexes
    conn.close()


def _seed_turn_usage(conn, rows):
    conn.execute("INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status) "
                 "VALUES('s1','agent','claude','anthropic',1,'completed')")
    conn.execute("INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,started_at_us,status) "
                 "VALUES('t1','s1',0,'user',1,1,'completed')")
    for measurement_id, scope, source, model, input_tokens, supersedes in rows:
        conn.execute(
            "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,model,"
            "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
            "observed_at_us,supersedes_measurement_id) VALUES(?,?,'t1',NULL,'anthropic',?,?,0,1,?,10,1,?)",
            (measurement_id, scope, model, source, input_tokens, supersedes))
    fill_model_keys(conn)
    conn.commit()


def test_best_usage_counts_one_model_under_two_spellings_once(tmp_path):
    # Claude Code's terminal usage says "claude-opus-5-5[1m]"; its transcript
    # says "claude-opus-5-5". They are one measurement, not two.
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    _seed_turn_usage(conn, [
        ("stream", "turn", "provider_stream", "claude-opus-5-5[1m]", 1000, None),
        ("transcript", "turn", "claude_transcript", "claude-opus-5-5", 1000, None),
    ])
    assert conn.execute("SELECT SUM(input_tokens) FROM best_usage").fetchone()[0] == 1000
    conn.close()


@pytest.mark.parametrize(("model", "key"), [
    ("claude-opus-5-5[1m]", "claude-opus-5-5"),
    ("Claude-Opus-5.5", "claude-opus-5-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("opus", "claude-opus-5-5"),
    ("cx/gpt-5.6-sol", "gpt-5.6-sol"),
])
def test_best_usage_keys_every_spelling_of_a_model_alike(tmp_path, model, key):
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    _seed_turn_usage(conn, [("u", "turn", "provider_stream", model, 10, None)])
    assert tuple(conn.execute("SELECT model, model_key FROM best_usage").fetchone()) == (model, key)
    conn.close()


def test_rows_written_without_a_key_never_merge_two_models(tmp_path):
    # An older writer leaves model_key NULL; until the next migrate keys it,
    # each row still counts as its own model.
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    conn.execute("INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status) "
                 "VALUES('s1','agent','claude','anthropic',1,'completed')")
    conn.execute("INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
                 "started_at_us,status) VALUES('t1','s1',0,'user',1,1,'completed')")
    for measurement_id, model, tokens in (("a", "claude-opus-5-5", 100), ("b", "claude-haiku-4-5", 7)):
        conn.execute("INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
                     "measurement_source,is_estimated,token_semantics_version,input_tokens,"
                     "output_tokens,observed_at_us) VALUES(?,'turn','t1','anthropic',?,"
                     "'provider_stream',0,1,?,1,1)", (measurement_id, model, tokens))
    conn.commit()
    assert conn.execute("SELECT SUM(input_tokens) FROM best_usage").fetchone()[0] == 107

    migrate(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM usage_measurements WHERE model_key IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(input_tokens) FROM best_usage").fetchone()[0] == 107
    conn.close()


def test_migrate_keys_rows_written_before_the_model_key_column(tmp_path):
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    conn.execute("DROP VIEW best_usage")
    conn.execute("DROP INDEX idx_usage_unkeyed")
    conn.execute("ALTER TABLE usage_measurements DROP COLUMN model_key")
    conn.execute("UPDATE schema_meta SET value='2' WHERE key='best_usage_version'")
    conn.commit()
    conn.execute("INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status) "
                 "VALUES('s1','agent','claude','anthropic',1,'completed')")
    conn.execute("INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
                 "started_at_us,status) VALUES('t1','s1',0,'user',1,1,'completed')")
    conn.execute("INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
                 "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
                 "observed_at_us) VALUES('u','turn','t1','anthropic','claude-opus-5-5[1m]',"
                 "'provider_stream',0,1,10,1,1)")
    conn.commit()

    migrate(conn)

    assert conn.execute("SELECT model_key FROM best_usage").fetchone()[0] == "claude-opus-5-5"
    conn.close()


def test_best_usage_keeps_claudes_turn_total_over_a_superseding_transcript(tmp_path):
    # The terminal total includes background calls the transcript never logs.
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    _seed_turn_usage(conn, [
        ("stream", "turn", "provider_stream", "claude-haiku-4-5-20251001", 1_108, None),
        ("transcript", "turn", "claude_transcript", "claude-haiku-4-5-20251001", 1_000, "stream"),
    ])
    assert [tuple(row) for row in conn.execute("SELECT measurement_id, input_tokens FROM best_usage")] == [
        ("stream", 1_108)]
    conn.close()


def test_migrate_rebuilds_a_stale_best_usage_view(tmp_path):
    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    conn.execute("DROP VIEW best_usage")
    conn.execute("CREATE VIEW best_usage AS SELECT 'stale' AS measurement_id")
    conn.execute("UPDATE schema_meta SET value='1' WHERE key='best_usage_version'")
    conn.commit()
    migrate(conn)
    assert "model_key" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='best_usage'").fetchone()[0]
    conn.close()


def test_migrate_refuses_a_newer_database_before_touching_it(tmp_path):
    from bobi.metrics.schema import SCHEMA_VERSION

    conn = connect(tmp_path / "metrics.db")
    migrate(conn)
    conn.execute("DROP VIEW best_usage")
    conn.execute("CREATE VIEW best_usage AS SELECT 'newer' AS measurement_id")
    conn.execute("UPDATE schema_meta SET value='999' WHERE key='best_usage_version'")
    conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.commit()

    with pytest.raises(RuntimeError, match="newer than this Bobi"):
        migrate(conn)

    assert "'newer'" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='best_usage'").fetchone()[0]
    assert dict(conn.execute("SELECT key, value FROM schema_meta").fetchall()) == {
        "best_usage_version": "999", "schema_version": str(SCHEMA_VERSION + 1)}
    conn.close()


def test_collector_projects_more_than_one_batch(tmp_path):
    segment = tmp_path / "many.telemetry"
    db = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        for sequence in range(750):
            writer.append(
                _store_event(
                    "session.recorded",
                    sequence,
                    {
                        "session_id": f"session-{sequence}",
                        "session_name": "agent",
                        "brain": "stub",
                        "provider": "stub",
                        "started_at_us": sequence + 1,
                        "status": "completed",
                    },
                    session_id=f"session-{sequence}",
                )
            )
    result = MetricsCollector(db).collect_segment(segment)
    assert result["staged"] == 750
    assert result["projected"] == 750
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 750
    conn.close()


def test_collector_commits_valid_prefix_before_corrupt_frame(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(
            _store_event(
                "session.recorded",
                1,
                {
                    "session_id": "s1",
                    "session_name": "agent",
                    "brain": "stub",
                    "provider": "stub",
                    "started_at_us": 1,
                    "status": "completed",
                },
                session_id="s1",
            )
        )
        corrupt_offset = writer.append(
            _store_event(
                "session.recorded",
                2,
                {
                    "session_id": "s2",
                    "session_name": "agent",
                    "brain": "stub",
                    "provider": "stub",
                    "started_at_us": 2,
                    "status": "completed",
                },
                session_id="s2",
            )
        )
    data = bytearray(segment.read_bytes())
    data[corrupt_offset + HEADER.size] ^= 1
    segment.write_bytes(data)

    with pytest.raises(SpoolCorruptionError, match="checksum"):
        MetricsCollector(db).collect_segment(segment)

    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute(
        "SELECT committed_offset FROM spool_cursors WHERE segment_path=?",
        (str(segment),),
    ).fetchone()[0] == corrupt_offset
    conn.close()


def test_duplicate_event_id_requires_identical_event_content(tmp_path):
    first_segment = tmp_path / "first.telemetry"
    second_segment = tmp_path / "second.telemetry"
    db = tmp_path / "metrics.db"
    original = _store_event(
        "session.recorded",
        1,
        {
            "session_id": "s1",
            "session_name": "agent",
            "brain": "stub",
            "provider": "stub",
            "started_at_us": 1,
            "status": "completed",
        },
        session_id="s1",
    )
    conflict = MetricsEvent(
        event_type=original.event_type,
        producer_id="another-producer",
        producer_sequence=1,
        source=original.source,
        payload={**original.payload, "status": "failed"},
        event_id=original.event_id,
        session_id=original.session_id,
    )
    with SpoolWriter(first_segment) as writer:
        writer.append(original)
    with SpoolWriter(second_segment) as writer:
        writer.append(conflict)

    MetricsCollector(db).collect_segment(first_segment)
    conn = connect(db)
    result = stage_frames(
        conn,
        segment_path=second_segment,
        frames=iter_frames(second_segment),
    )
    # The first event stands; the conflicting one is set aside, not imported,
    # and the cursor still moves past it.
    assert (result.staged, result.conflicts) == (0, 1)
    assert conn.execute("SELECT payload_json FROM raw_events").fetchone()[0].count('"completed"') == 1
    assert cursor_offset(conn, second_segment) == second_segment.stat().st_size
    conn.close()


def test_identical_event_id_replay_from_another_segment_is_a_noop(tmp_path):
    first_segment = tmp_path / "first.telemetry"
    second_segment = tmp_path / "second.telemetry"
    db = tmp_path / "metrics.db"
    original = _store_event(
        "session.recorded",
        1,
        {
            "session_id": "s1",
            "session_name": "agent",
            "brain": "stub",
            "provider": "stub",
            "started_at_us": 1,
            "status": "completed",
        },
        session_id="s1",
    )
    replay = MetricsEvent(
        event_type=original.event_type,
        producer_id="reconciler",
        producer_sequence=9,
        source=original.source,
        payload=original.payload,
        event_id=original.event_id,
        emitted_at_us=original.emitted_at_us,
        session_id=original.session_id,
    )
    with SpoolWriter(first_segment) as writer:
        writer.append(original)
    with SpoolWriter(second_segment) as writer:
        writer.append(replay)

    first = MetricsCollector(db).collect_segment(first_segment)
    second = MetricsCollector(db).collect_segment(second_segment)
    assert first["staged"] == 1
    assert second["staged"] == 0
    assert second["projected"] == 0
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
    conn.close()


def _projection_event(event_type, sequence, payload, **ids):
    return MetricsEvent(
        event_type=event_type,
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload=payload,
        **ids,
    )


def test_historical_session_assignment_survives_projection(tmp_path):
    assignment = historical_assignment()
    segment = tmp_path / "historical.telemetry"
    database = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(_projection_event("session.recorded", 1, {
            "session_id": "historical-session", "session_name": "agent",
            "brain": "codex", "provider": "openai", "started_at_us": 1,
            "status": "running",
            "metadata_json": json.dumps({"router_assignment": assignment}),
        }, session_id="historical-session"))
        writer.append(_projection_event("turn.recorded", 2, {
            "turn_id": "historical-turn", "session_id": "historical-session",
            "turn_index": 1, "trigger_kind": "user", "is_user_initiated": 1,
            "started_at_us": 2, "status": "completed",
        }, session_id="historical-session", turn_id="historical-turn"))
    MetricsCollector(database).collect_segment(segment)
    conn = connect(database, readonly=True)
    try:
        decision = conn.execute("SELECT * FROM router_decisions").fetchone()
        assert decision["variant_id"] == assignment["variant_id"]
        assert decision["assignment_key_hash"] == assignment["assignment_key_hash"]
        assert decision["model_selected"] == assignment["model_selected"]
        assert decision["decided_at_us"] == 2
    finally:
        conn.close()


def test_child_stages_before_parent_and_projects_after_parent_arrives(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    child = _projection_event(
        "usage.recorded",
        1,
        {
            "measurement_id": "u1",
            "scope": "turn",
            "turn_id": "t1",
            "provider": "openai",
            "model": "gpt-test",
            "measurement_source": "provider_stream",
            "is_estimated": 0,
            "token_semantics_version": 1,
            "input_tokens": 4,
            "output_tokens": 2,
            "observed_at_us": 3,
        },
        session_id="s1",
        turn_id="t1",
    )
    with SpoolWriter(segment) as writer:
        writer.append(child)
    result = MetricsCollector(db).collect_segment(segment)
    assert result["staged"] == 1
    assert result["deferred"] == 1

    with SpoolWriter(segment) as writer:
        writer.append(
            _projection_event(
                "session.recorded",
                2,
                {
                    "session_id": "s1",
                    "session_name": "agent",
                    "brain": "codex",
                    "provider": "openai",
                    "started_at_us": 1,
                    "status": "running",
                },
                session_id="s1",
            )
        )
        writer.append(
            _projection_event(
                "turn.recorded",
                3,
                {
                    "turn_id": "t1",
                    "session_id": "s1",
                    "turn_index": 1,
                    "trigger_kind": "user",
                    "is_user_initiated": 1,
                    "started_at_us": 2,
                    "status": "completed",
                },
                session_id="s1",
                turn_id="t1",
            )
        )
    resumed = MetricsCollector(db).collect_segment(segment)
    assert resumed["projected"] == 3
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM best_usage").fetchone()[0] == 1
    conn.close()


def test_missing_parent_is_quarantined_after_orphan_ttl(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(_projection_event(
            "usage.recorded",
            1,
            {
                "measurement_id": "orphan",
                "scope": "turn",
                "turn_id": "missing-turn",
                "provider": "openai",
                "model": "gpt-test",
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "token_semantics_version": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "observed_at_us": 1,
            },
            session_id="missing-session",
            turn_id="missing-turn",
        ))
    MetricsCollector(db).collect_segment(segment)
    conn = connect(db)
    received = conn.execute(
        "SELECT received_at_us FROM raw_events WHERE event_id IS NOT NULL"
    ).fetchone()[0]

    result = project_pending(conn, now_us=received + ORPHAN_TTL_US)
    row = conn.execute(
        "SELECT projection_state,projection_attempts,projection_not_before_us,"
        "projection_error FROM raw_events"
    ).fetchone()
    conn.close()

    assert result["quarantined"] == 1
    assert tuple(row) == (
        "quarantined",
        2,
        None,
        "missing_parent_ttl_expired",
    )

    with SpoolWriter(segment) as writer:
        writer.append(_projection_event(
            "session.recorded",
            2,
            {
                "session_id": "missing-session",
                "session_name": "agent",
                "brain": "codex",
                "provider": "openai",
                "started_at_us": 1,
                "status": "running",
            },
            session_id="missing-session",
        ))
        writer.append(_projection_event(
            "turn.recorded",
            3,
            {
                "turn_id": "missing-turn",
                "session_id": "missing-session",
                "turn_index": 1,
                "trigger_kind": "user",
                "is_user_initiated": 1,
                "started_at_us": 2,
                "status": "completed",
            },
            session_id="missing-session",
            turn_id="missing-turn",
        ))

    resumed = MetricsCollector(db).collect_segment(segment)
    conn = connect(db, readonly=True)
    try:
        orphan = conn.execute(
            "SELECT projection_state FROM raw_events WHERE event_type='usage.recorded'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert resumed["projected"] == 3
    assert orphan == "projected"


def test_unrelated_parent_does_not_requeue_ttl_quarantined_orphan(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(_projection_event(
            "usage.recorded",
            1,
            {
                "measurement_id": "orphan",
                "scope": "turn",
                "turn_id": "missing-turn",
                "provider": "openai",
                "model": "gpt-test",
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "token_semantics_version": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "observed_at_us": 1,
            },
            session_id="missing-session",
            turn_id="missing-turn",
        ))
    MetricsCollector(db).collect_segment(segment)
    conn = connect(db)
    received = conn.execute(
        "SELECT received_at_us FROM raw_events WHERE event_id IS NOT NULL"
    ).fetchone()[0]
    project_pending(conn, now_us=received + ORPHAN_TTL_US)
    conn.close()

    with SpoolWriter(segment) as writer:
        writer.append(_projection_event(
            "session.recorded",
            2,
            {
                "session_id": "unrelated-session",
                "session_name": "unrelated",
                "brain": "codex",
                "provider": "openai",
                "started_at_us": 1,
                "status": "running",
            },
            session_id="unrelated-session",
        ))

    MetricsCollector(db).collect_segment(segment)
    conn = connect(db, readonly=True)
    try:
        orphan = conn.execute(
            "SELECT projection_state,projection_error FROM raw_events "
            "WHERE event_type='usage.recorded'"
        ).fetchone()
    finally:
        conn.close()

    assert tuple(orphan) == ("quarantined", "missing_parent_ttl_expired")


def test_same_producer_lifecycle_events_project_in_emission_order(tmp_path):
    db = tmp_path / "metrics.db"
    segment = tmp_path / "segment.telemetry"
    common = {
        "turn_id": "t1",
        "session_id": "s1",
        "turn_index": 1,
        "trigger_kind": "user",
        "is_user_initiated": 1,
        "started_at_us": 2,
    }
    events = [
        MetricsEvent(
            event_type="session.recorded",
            producer_id="producer",
            producer_sequence=0,
            source="test",
            event_id="evt_session",
            emitted_at_us=1,
            session_id="s1",
            payload={
                "session_id": "s1",
                "session_name": "agent",
                "brain": "claude",
                "provider": "anthropic",
                "started_at_us": 1,
                "status": "running",
            },
        ),
        MetricsEvent(
            event_type="turn.recorded",
            producer_id="producer",
            producer_sequence=1,
            source="test",
            event_id="evt_z_running",
            emitted_at_us=2,
            session_id="s1",
            turn_id="t1",
            payload={**common, "status": "running"},
        ),
        MetricsEvent(
            event_type="turn.recorded",
            producer_id="producer",
            producer_sequence=2,
            source="test",
            event_id="evt_a_completed",
            emitted_at_us=3,
            session_id="s1",
            turn_id="t1",
            payload={**common, "status": "completed", "ended_at_us": 3},
        ),
    ]
    with SpoolWriter(segment) as writer:
        for event in events:
            writer.append(event)

    MetricsCollector(db).collect_segment(segment)

    conn = connect(db, readonly=True)
    try:
        row = conn.execute(
            "SELECT status, ended_at_us FROM turns WHERE turn_id='t1'"
        ).fetchone()
        assert tuple(row) == ("completed", 3)
    finally:
        conn.close()


def test_exact_measurement_wins_without_deleting_estimate(tmp_path):
    db = tmp_path / "metrics.db"
    segment = tmp_path / "segment.telemetry"
    session = {
        "session_id": "s1",
        "session_name": "agent",
        "brain": "claude",
        "provider": "anthropic",
        "started_at_us": 1,
        "status": "running",
    }
    turn = {
        "turn_id": "t1",
        "session_id": "s1",
        "turn_index": 1,
        "trigger_kind": "user",
        "is_user_initiated": 1,
        "started_at_us": 2,
        "status": "completed",
    }
    base = {
        "scope": "turn",
        "turn_id": "t1",
        "provider": "anthropic",
        "model": "claude-test",
        "token_semantics_version": 1,
        "observed_at_us": 3,
    }
    estimate = dict(
        base,
        measurement_id="estimated",
        measurement_source="calibrated_estimator",
        is_estimated=1,
        input_tokens=11,
        output_tokens=4,
    )
    exact = dict(
        base,
        measurement_id="exact",
        measurement_source="provider_stream",
        is_estimated=0,
        input_tokens=10,
        output_tokens=3,
    )
    events = [
        _projection_event("session.recorded", 1, session, session_id="s1"),
        _projection_event("turn.recorded", 2, turn, session_id="s1", turn_id="t1"),
        _projection_event("usage.recorded", 3, estimate, session_id="s1", turn_id="t1"),
        _projection_event("usage.recorded", 4, exact, session_id="s1", turn_id="t1"),
    ]
    with SpoolWriter(segment) as writer:
        for item in events:
            writer.append(item)
    MetricsCollector(db).collect_segment(segment)
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0] == 2
    row = conn.execute("SELECT measurement_id FROM best_usage").fetchone()
    assert row[0] == "exact"
    conn.close()


def test_a_config_change_under_one_experiment_id_keeps_every_routed_turn(tmp_path):
    # Saving the JEV config keeps the experiment id; both decisions must project.
    db = tmp_path / "metrics.db"
    segment = tmp_path / "segment.telemetry"
    session = {
        "session_id": "s1", "session_name": "agent", "brain": "claude",
        "provider": "anthropic", "started_at_us": 1, "status": "running",
    }
    turns = [
        {
            "turn_id": turn_id, "session_id": "s1", "turn_index": index,
            "trigger_kind": "user", "is_user_initiated": 1,
            "started_at_us": index + 1, "status": "completed",
        }
        for index, turn_id in enumerate(("t1", "t2"), 1)
    ]
    decision = {
        "router_decision_id": "r1", "turn_id": "t1", "experiment_id": "exp",
        "variant_id": "control", "assignment_status": "assigned",
        "router_name": "jev", "router_version": "1", "policy_version": "p1",
        "feature_schema_version": "f1", "candidate_models_json": '["a","b"]',
        "model_selected": "a", "control_model": "a", "router_latency_ms": 1,
        "decided_at_us": 2, "metadata_json": '{"config_fingerprint":"one"}',
    }
    changed = {
        **decision,
        "router_decision_id": "r2", "turn_id": "t2", "policy_version": "p2",
        "decided_at_us": 3, "metadata_json": '{"config_fingerprint":"two"}',
    }
    with SpoolWriter(segment) as writer:
        writer.append(_projection_event("session.recorded", 1, session, session_id="s1"))
        writer.append(_projection_event("turn.recorded", 2, turns[0], session_id="s1", turn_id="t1"))
        writer.append(_projection_event("router_decision.recorded", 3, decision, session_id="s1", turn_id="t1"))
        writer.append(_projection_event("turn.recorded", 4, turns[1], session_id="s1", turn_id="t2"))
        writer.append(_projection_event("router_decision.recorded", 5, changed, session_id="s1", turn_id="t2"))

    result = MetricsCollector(db).collect_segment(segment)
    conn = connect(db, readonly=True)
    try:
        assert result["quarantined"] == 0
        assert [tuple(row) for row in conn.execute(
            "SELECT router_decision_id, policy_version FROM router_decisions ORDER BY decided_at_us")] == [
            ("r1", "p1"), ("r2", "p2")]
    finally:
        conn.close()


def _us(value: str) -> int:
    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
        * 1_000_000
    )


def _reconcile_event(event_type, sequence, payload, **ids):
    return MetricsEvent(
        event_type=event_type,
        producer_id="fixture",
        producer_sequence=sequence,
        source="test",
        payload=payload,
        **ids,
    )


def _seed_turn(root, *, provider, brain, provider_session_id, turn_id, estimate):
    segment = root / "state" / "metrics" / "spool" / "fixture" / "one.telemetry"
    session_id = f"session-{provider}"
    started = _us("2026-09-24T23:59:59Z")
    ended = _us("2026-09-25T00:00:02Z")
    events = [
        _reconcile_event(
            "session.recorded",
            1,
            {
                "session_id": session_id,
                "session_name": "agent",
                "provider_session_id": provider_session_id,
                "brain": brain,
                "provider": provider,
                "started_at_us": started,
                "status": "running",
            },
            session_id=session_id,
        ),
        _reconcile_event(
            "turn.recorded",
            2,
            {
                "turn_id": turn_id,
                "session_id": session_id,
                "turn_index": 1,
                "trigger_kind": "inbox",
                "is_user_initiated": 1,
                "started_at_us": started,
                "ended_at_us": ended,
                "status": "completed",
            },
            session_id=session_id,
            turn_id=turn_id,
        ),
    ]
    if estimate is not None:
        events.append(_reconcile_event(
            "usage.recorded",
            3,
            estimate,
            session_id=session_id,
            turn_id=turn_id,
        ))
    with SpoolWriter(segment) as writer:
        for event in events:
            writer.append(event)
    MetricsCollector(root / "state" / "metrics" / "metrics.db").collect_segment(
        segment
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_gateway_reconciliation_rejects_unknown_transcript_format(metrics_fixtures, monkeypatch, tmp_path, ambiguous):
    _seed_turn(tmp_path, provider="gateway", brain="gateway",
               provider_session_id="fixture-gateway", turn_id="turn-gateway", estimate=None)
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda session_id:
                        metrics_fixtures / "claude-transcript.jsonl" if ambiguous else None)
    monkeypatch.setattr("bobi.metrics.reconcile.find_codex_rollout", lambda session_id:
                        metrics_fixtures / "codex-rollout.jsonl" if ambiguous else None)
    with pytest.raises(ReconciliationError, match="missing or ambiguous"):
        reconcile_turn(tmp_path, "turn-gateway", wait=True)


def test_reconciling_again_after_the_transcript_grew_adds_the_new_call(monkeypatch, tmp_path):
    def call(message_id, at):
        return json.dumps({"isSidechain": False, "type": "assistant", "sessionId": "grow", "timestamp": at,
                           "uuid": f"entry-{message_id}", "requestId": f"req-{message_id}",
                           "message": {"id": message_id, "model": "claude-opus-4-8", "role": "assistant",
                                       "type": "message", "content": [],
                                       "usage": {"input_tokens": 5, "output_tokens": 100,
                                                 "cache_read_input_tokens": 0,
                                                 "cache_creation_input_tokens": 0}}}) + "\n"

    _seed_turn(tmp_path, provider="anthropic", brain="claude", provider_session_id="grow",
               turn_id="turn-grow", estimate=None)
    transcript = tmp_path / "grow.jsonl"
    transcript.write_text(call("msg-a", "2026-09-25T00:00:00.000Z"))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda session_id: transcript)
    reconcile_turn(tmp_path, "turn-grow", wait=True)
    with transcript.open("a") as handle:
        handle.write(call("msg-b", "2026-09-25T00:00:01.000Z"))
    second = reconcile_turn(tmp_path, "turn-grow", wait=True)

    assert second["status"] == "done"
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        assert conn.execute("SELECT output_tokens FROM usage_measurements WHERE turn_id='turn-grow' "
                            "AND scope='turn' AND measurement_source='claude_transcript'").fetchone()[0] == 200
        assert conn.execute("SELECT COUNT(*) FROM usage_measurements WHERE turn_id='turn-grow' "
                            "AND scope='invocation'").fetchone()[0] == 2
    finally:
        conn.close()


def test_a_running_turn_is_not_reconciled(tmp_path):
    _seed_turn(tmp_path, provider="anthropic", brain="claude", provider_session_id="grow",
               turn_id="turn-running", estimate=None)
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE turns SET ended_at_us=NULL, status='running', started_at_us=?", (time.time_ns() // 1000,))
    conn.commit()
    conn.close()
    with pytest.raises(ReconciliationError, match="still running"):
        reconcile_turn(tmp_path, "turn-running")


def test_provider_record_parsers_preserve_correlation_and_deduplicate(metrics_fixtures, ):
    claude = parse_claude_transcript_records(metrics_fixtures / "claude-transcript.jsonl")
    codex = parse_codex_rollout_records(metrics_fixtures / "codex-rollout.jsonl")

    assert [record.usage.provider_event_id for record in claude] == [
        "msg-fixture-1",
        "msg-fixture-sidechain",
    ]
    assert all(record.provider_session_id == "fixture-claude-session" for record in claude)
    assert len(codex) == 1
    assert codex[0].provider_session_id == "fixture-codex-thread"
    assert codex[0].provider_turn_id == "fixture-codex-turn"
    assert codex[0].usage.input_tokens == 4096


def _reconcile_attempts(root):
    conn = connect(root / "state" / "metrics" / "metrics.db")
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT turn_id, attempts FROM reconcile_attempts")}
    finally:
        conn.close()


@pytest.mark.parametrize("error", [ReconciliationError, ProviderContractError, OSError])
def test_background_reconciliation_retries_are_bounded_and_persisted(monkeypatch, tmp_path, error):
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.reconcile import ReconciliationError

    _seed_turn(tmp_path, provider="gateway", brain="claude", provider_session_id="missing-session",
               turn_id="missing-turn", estimate=None)
    calls = []

    def unavailable(root, turn_id, **kwargs):
        calls.append(turn_id)
        raise error("transcript not available")

    monkeypatch.setattr("bobi.metrics.reconcile.reconcile_turn", unavailable)
    for index in range(5):
        service = MetricsCollectorService(tmp_path, reconcile_interval=0)
        service._cycle_locked()
        service._publish_health(force=True)
    assert calls == ["missing-turn"] * 3
    assert service.health()["reconciliation_retry_exhausted_turns"] == 1
    # Per-turn counts live in SQLite, never in health or the heartbeat.
    assert "reconciliation_attempts" not in service.health()
    assert _reconcile_attempts(tmp_path) == {"missing-turn": 3}
    assert reconcile_missing(tmp_path)["errors"] == 1
    assert len(calls) == 4


def test_a_turn_recovered_by_hand_stops_counting_as_exhausted(monkeypatch, tmp_path):
    from bobi.metrics.collector import MetricsCollectorService

    _seed_turn(tmp_path, provider="gateway", brain="claude", provider_session_id="missing-session",
               turn_id="missing-turn", estimate=None)

    def unavailable(root, turn_id, **kwargs):
        raise ReconciliationError("transcript not available")

    monkeypatch.setattr("bobi.metrics.reconcile.reconcile_turn", unavailable)
    for _ in range(3):
        MetricsCollectorService(tmp_path, reconcile_interval=0)._cycle_locked()
    assert _reconcile_attempts(tmp_path) == {"missing-turn": 3}
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db")
    conn.execute("INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,measurement_source,"
                 "is_estimated,token_semantics_version,input_tokens,output_tokens,observed_at_us) "
                 "VALUES('manual','turn','missing-turn','gateway','m','claude_transcript',0,1,1,1,1)")
    conn.commit()
    conn.close()
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    service._cycle_locked()
    assert service.health()["reconciliation_retry_exhausted_turns"] == 0
    assert _reconcile_attempts(tmp_path) == {}


def test_standby_collector_preserves_retry_limit_on_takeover(monkeypatch, tmp_path):
    from bobi.metrics.collector import MetricsCollectorService

    _seed_turn(tmp_path, provider="gateway", brain="claude", provider_session_id="missing-session",
               turn_id="missing-turn", estimate=None)
    calls = []
    def unavailable(root, turn_id, **kwargs):
        calls.append(turn_id)
        raise ReconciliationError("transcript not available")
    monkeypatch.setattr("bobi.metrics.reconcile.reconcile_turn", unavailable)
    first = MetricsCollectorService(tmp_path, poll_interval=0.01, reconcile_interval=0)
    second = MetricsCollectorService(tmp_path, poll_interval=0.01, reconcile_interval=0)
    try:
        first.start()
        deadline = time.monotonic() + 2
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 3
        first._publish_health(force=True)
        second.start()
        deadline = time.monotonic() + 2
        while second.health()["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert second.health()["role"] == "standby"
        assert _reconcile_attempts(tmp_path) == {"missing-turn": 3}
        assert first.stop(timeout=2)
        deadline = time.monotonic() + 2
        while second.health()["role"] != "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert second.health()["role"] == "active"
        assert len(calls) == 3
    finally:
        assert first.stop(timeout=2)
        assert second.stop(timeout=2)


def test_codex_v0159_rollout_parser_converts_cumulative_usage_to_turn_deltas(metrics_fixtures, ):
    records = parse_codex_rollout_records(
        metrics_fixtures / "codex-rollout-v0159.jsonl"
    )

    assert [record.provider_turn_id for record in records] == [
        "turn-v0159-1",
        "turn-v0159-2",
    ]
    assert records[0].provider_session_id == "fixture-codex-v0159"
    assert records[0].usage.input_tokens == 150
    assert records[0].usage.cache_read_input_tokens == 60
    assert records[0].usage.output_tokens == 15
    assert records[1].usage.input_tokens == 80
    assert records[1].usage.cache_read_input_tokens == 30
    assert records[1].usage.output_tokens == 7
    assert records[1].usage.reasoning_output_tokens == 2


def test_reconciled_turn_aggregate_preserves_unreported_dimensions_as_null(metrics_fixtures, ):
    record = parse_claude_transcript_records(
        metrics_fixtures / "claude-transcript.jsonl"
    )[0]
    complete = replace(
        record,
        usage=replace(
            record.usage,
            cache_write_5m_input_tokens=3,
            cache_write_1h_input_tokens=0,
            cache_write_unknown_ttl_input_tokens=None,
            cache_write_breakdown_complete=True,
            output_tokens=4,
        ),
    )
    partial = replace(
        record,
        usage=replace(
            record.usage,
            provider_event_id="partial-message",
            cache_write_5m_input_tokens=None,
            cache_write_1h_input_tokens=None,
            cache_write_unknown_ttl_input_tokens=5,
            cache_write_breakdown_complete=False,
            output_tokens=None,
        ),
    )

    aggregate = _aggregate([complete, partial])[0].usage

    assert aggregate.output_tokens is None
    assert aggregate.cache_write_5m_input_tokens is None
    assert aggregate.cache_write_unknown_ttl_input_tokens == 5
    assert aggregate.cache_write_breakdown_complete is False


def test_codex_rollout_from_the_pinned_cli_reconciles(metrics_fixtures):
    [record] = parse_codex_rollout_records(metrics_fixtures / "codex-rollout-v0144.jsonl")
    assert (record.provider_session_id, record.provider_turn_id) == ("fixture-codex-v0144", "turn-v0144-1")
    assert record.usage.model == "gpt-5.6-luna"
    assert (record.usage.input_tokens, record.usage.cache_read_input_tokens,
            record.usage.output_tokens) == (13364, 8960, 5)


@pytest.mark.parametrize("fixture", ["codex-rollout.jsonl", "codex-rollout-v0159.jsonl"])
def test_codex_rollout_parser_follows_the_shape_not_the_version(metrics_fixtures, tmp_path, fixture):
    lines = (metrics_fixtures / fixture).read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"]["cli_version"] = "9.0.0"
    target = tmp_path / "rollout.jsonl"
    target.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
    assert parse_codex_rollout_records(target) == parse_codex_rollout_records(metrics_fixtures / fixture)


def _rollout(tmp_path, *events):
    target = tmp_path / "rollout.jsonl"
    target.write_text("\n".join(json.dumps({"timestamp": f"2026-09-25T00:00:{index:02d}.000Z", **event})
                                for index, event in enumerate(events)) + "\n")
    return target


def _event(payload):
    return {"type": "event_msg", "payload": payload}


def test_codex_rollout_with_a_completed_turn_but_no_known_usage_shape_is_rejected(tmp_path):
    started = [{"type": "session_meta", "payload": {"id": "fixture", "cli_version": "9.0.0"}},
               _event({"type": "task_started", "turn_id": "turn-1"}),
               _event({"type": "usage_v2", "tokens": {"in": 1, "out": 1}})]
    # A turn still running has no usage yet; that is not a format change.
    assert parse_codex_rollout_records(_rollout(tmp_path, *started)) == []
    completed = _rollout(tmp_path, *started, _event({"type": "task_complete", "turn_id": "turn-1"}))
    with pytest.raises(ProviderContractError, match="no usage in a known shape.*9.0.0"):
        parse_codex_rollout_records(completed)


def test_codex_snapshot_with_no_known_usage_key_is_rejected(tmp_path):
    rollout = _rollout(
        tmp_path,
        {"type": "session_meta", "payload": {"id": "fixture", "cli_version": "9.0.0"}},
        _event({"type": "task_started", "turn_id": "t1"}),
        _event({"type": "token_count", "info": {"total_token_usage": {"input": 13364, "cached": 8960, "output": 5}}}),
        _event({"type": "task_complete", "turn_id": "t1"}),
    )
    with pytest.raises(ProviderContractError, match="no usage in a known shape"):
        parse_codex_rollout_records(rollout)


def test_codex_rollout_mixing_both_usage_shapes_keeps_every_turn(tmp_path):
    def snapshot(total):
        return _event({"type": "token_count", "info": {"total_token_usage": {"input_tokens": total, "output_tokens": 1}}})
    records = parse_codex_rollout_records(_rollout(
        tmp_path,
        {"type": "session_meta", "payload": {"id": "fixture", "cli_version": "9.0.0"}},
        _event({"type": "task_started", "turn_id": "t1"}),
        snapshot(100),
        {"type": "token_usage_record", "payload": {"turn_id": "t1",
                                                  "turn_token_usage": {"input_tokens": 100, "output_tokens": 1}}},
        _event({"type": "task_started", "turn_id": "t2"}),
        snapshot(250),
        _event({"type": "task_complete", "turn_id": "t2"}),
        # A turn still running has a partial snapshot and no record yet.
        _event({"type": "task_started", "turn_id": "t3"}),
        snapshot(260),
    ))
    assert [(record.provider_turn_id, record.usage.input_tokens) for record in records] == [("t1", 100), ("t2", 150)]


_MINUTE_US = 60 * 1_000_000


def _claude_call(session_id, message_id, at_us, output_tokens=100):
    at = datetime.fromtimestamp(at_us / 1_000_000, tz=timezone.utc)
    return json.dumps({"isSidechain": False, "type": "assistant", "sessionId": session_id,
                       "timestamp": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                       "uuid": f"entry-{message_id}", "requestId": f"req-{message_id}",
                       "message": {"id": message_id, "model": "claude-opus-4-8", "role": "assistant",
                                   "type": "message", "content": [],
                                   "usage": {"input_tokens": 5, "output_tokens": output_tokens,
                                             "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": 0}}}) + "\n"


def _claude_result(session_id, message_ids):
    return TurnResult(
        session_id=session_id,
        usage=[BrainUsage(model="claude-opus-4-8", input_tokens=5 * len(message_ids),
                          output_tokens=100 * len(message_ids))],
        invocations=[BrainInvocation(provider_event_id=message_id, model="claude-opus-4-8")
                     for message_id in message_ids],
    )


def _flush(runtime):
    """Wait until the runtime's producer has written every accepted event."""
    deadline = time.monotonic() + 5
    while runtime._producer.events_written < runtime.events_emitted:
        assert time.monotonic() < deadline, "producer did not flush"
        time.sleep(0.01)


def _dead_pid():
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


def _dead_runtime(monkeypatch, root):
    """A runtime whose process has since died, as after a SIGKILL."""
    with monkeypatch.context() as patch:
        patch.setattr("bobi.metrics.runtime.os.getpid", _dead_pid)
        return MetricsRuntime(root, mode="enabled")


def _turn_usage(root):
    conn = connect(root / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        return {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT turn_id,measurement_source,output_tokens FROM best_usage WHERE scope='turn'")}
    finally:
        conn.close()


def test_a_turn_running_past_an_hour_is_left_to_its_live_writer(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    started = time.time_ns() // 1000 - 61 * _MINUTE_US
    transcript = tmp_path / "live.jsonl"
    transcript.write_text("".join(_claude_call("live", f"msg-{index}", started + index * 1_000_000)
                                  for index in (1, 2, 3)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        turn = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
        _flush(runtime)
        # A fresh session's first turn has no provider session id while it runs.
        for _ in range(3):
            service._cycle_locked()
        assert service.health()["reconciliation_retry_exhausted_turns"] == 0
        save_session_id("agent", "live", root=tmp_path)
        service._cycle_locked()
        assert turn.turn_id not in _turn_usage(tmp_path)
        with pytest.raises(ReconciliationError, match="still running"):
            reconcile_turn(tmp_path, turn.turn_id)

        with transcript.open("a") as handle:
            handle.write("".join(_claude_call("live", f"msg-{index}", started + index * 1_000_000)
                                 for index in (4, 5)))
        turn.record_result(_claude_result("live", [f"msg-{index}" for index in range(1, 6)]))
        turn.finish(status="completed")
        _flush(runtime)
        service._cycle_locked()
    finally:
        runtime.close()

    assert _turn_usage(tmp_path)[turn.turn_id][1] == 500
    health = service.health()
    assert health["quarantined_events"] == 0
    assert health["reconciliation_retry_exhausted_turns"] == 0
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM llm_invocations WHERE turn_id=?",
                            (turn.turn_id,)).fetchone()[0] == 5
    finally:
        conn.close()


def test_a_turn_whose_writer_died_is_reconciled_at_once(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    started = time.time_ns() // 1000 - _MINUTE_US
    transcript = tmp_path / "crashed.jsonl"
    transcript.write_text("".join(_claude_call("crashed", f"msg-{index}", started + index * 1_000_000)
                                  for index in (1, 2)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "crashed", root=tmp_path)
    runtime = _dead_runtime(monkeypatch, tmp_path)
    turn = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
    _flush(runtime)
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    service._cycle_locked()

    assert _turn_usage(tmp_path)[turn.turn_id] == ("claude_transcript", 200)


def test_a_turn_whose_writer_is_unknown_is_not_taken_as_abandoned(monkeypatch, tmp_path):
    _seed_turn(tmp_path, provider="anthropic", brain="claude", provider_session_id="grow",
               turn_id="turn-unowned", estimate=None)
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE turns SET ended_at_us=NULL, status='running'")
    conn.commit()
    conn.close()

    assert reconcile_missing(tmp_path, terminal_only=True)["turns_considered"] == 0
    with pytest.raises(ReconciliationError, match="still running"):
        reconcile_turn(tmp_path, "turn-unowned")


def _reused_pid_runtime(monkeypatch, root):
    """A runtime from a previous container life whose pid this process now
    holds, as after a restart in a fresh pid namespace: same pid, another
    process start value."""
    from bobi.metrics.producer import process_start_token

    with monkeypatch.context() as patch:
        patch.setattr("bobi.metrics.runtime.process_start_token",
                      lambda pid: process_start_token(pid) - 1)
        return MetricsRuntime(root, mode="enabled")


def test_a_reused_pid_reads_as_another_process(monkeypatch, tmp_path):
    from bobi.metrics.collector import producer_exited
    from bobi.metrics.events import uuid7
    from bobi.metrics.producer import process_start_token

    start = process_start_token(os.getpid())
    assert start is not None
    live = MetricsRuntime(tmp_path, mode="disabled").producer_id
    assert live.endswith(f"-{start}")
    assert producer_exited(live) is False
    assert producer_exited(_reused_pid_runtime(monkeypatch, tmp_path).producer_id) is True
    # An id without a start value can only be checked by its pid.
    assert producer_exited(f"producer-{os.getpid()}-{uuid7()}") is False
    assert producer_exited(f"producer-{os.getpid()}-z") is False


def test_process_start_is_read_from_proc_stat(tmp_path):
    from bobi.metrics.producer import _proc_start_ticks

    (tmp_path / "42").mkdir()
    # A command name may hold spaces and parentheses.
    fields = ["S"] + ["0"] * 18 + ["98765"] + ["0"] * 10
    (tmp_path / "42" / "stat").write_text(f"42 (odd) name) {' '.join(fields)}\n")

    assert _proc_start_ticks(42, proc=tmp_path) == 98765
    assert _proc_start_ticks(43, proc=tmp_path) is None


def test_a_wall_clock_step_does_not_make_a_live_writer_read_as_gone(monkeypatch, tmp_path):
    from bobi.metrics.collector import producer_exited

    real_time_ns = time.time_ns
    with monkeypatch.context() as patch:
        # The clock read 60 s behind when the writer started, and was then
        # stepped forward: it is the same process throughout.
        patch.setattr(time, "time_ns", lambda: real_time_ns() - 60 * 1_000_000_000)
        runtime = MetricsRuntime(tmp_path, mode="enabled")
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    try:
        runtime.begin_turn("agent", provider="anthropic", brain="claude")
        _flush(runtime)
        service.collect_once()
        segment = next(service.spool_root.glob("producer-*/*.telemetry"))
        _age(segment, 10 * 60)
        assert producer_exited(runtime.producer_id) is False
        service._cycle_locked()
        assert segment.exists()
        assert reconcile_missing(tmp_path, terminal_only=True)["turns_considered"] == 0

        runtime.begin_turn("other", provider="anthropic", brain="claude")
        _flush(runtime)
        service.collect_once()
    finally:
        runtime.close()
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
    finally:
        conn.close()


def test_a_corrupt_frame_does_not_hold_back_a_dead_writers_turn(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from bobi.cli import metrics
    from bobi.sdk import save_session_id

    started = time.time_ns() // 1000 - _MINUTE_US
    transcript = tmp_path / "corrupt.jsonl"
    transcript.write_text("".join(_claude_call("corrupt", f"msg-{index}", started + index * 1_000_000)
                                  for index in (1, 2)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    save_session_id("agent", "corrupt", root=tmp_path)
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
    _flush(crashed)
    crashed.close()
    segment = next((tmp_path / "state" / "metrics" / "spool").glob("producer-*/*.telemetry"))
    with segment.open("ab") as handle:
        handle.write(bytes(200))  # a complete header with a bad magic
    service = MetricsCollectorService(tmp_path)
    for _ in range(3):
        service._cycle_locked()

    result = CliRunner().invoke(metrics, ["reconcile", "--turn-id", turn.turn_id, "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert _turn_usage(tmp_path)[turn.turn_id] == ("claude_transcript", 200)


def _crash_before_restart(monkeypatch, root, started):
    """Turn A makes two calls in the previous life, then that process is killed."""
    from bobi.sdk import save_session_id

    transcript = root / "restart.jsonl"
    transcript.write_text("".join(_claude_call("restart", f"msg-a{index}", started + index * 1_000_000)
                                  for index in (1, 2)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "restart", root=root)
    previous_life = _reused_pid_runtime(monkeypatch, root)
    turn_a = previous_life.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
    _flush(previous_life)
    previous_life.close()
    return transcript, turn_a


def test_a_turn_cut_off_by_a_restart_is_reconciled_although_its_pid_is_reused(monkeypatch, tmp_path):
    now = time.time_ns() // 1000
    transcript, turn_a = _crash_before_restart(monkeypatch, tmp_path, now - 10 * _MINUTE_US)
    # The next life holds the same pid, resumes the session and runs turn B.
    b_start = now - _MINUTE_US
    with transcript.open("a") as handle:
        handle.write("".join(_claude_call("restart", f"msg-b{index}", b_start + index * 1_000_000)
                             for index in (1, 2)))
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    try:
        turn_b = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        turn_b.record_result(_claude_result("restart", ["msg-b1", "msg-b2"]))
        turn_b.finish(status="completed")
        _flush(runtime)
        service._cycle_locked()
    finally:
        runtime.close()

    usage = _turn_usage(tmp_path)
    assert usage[turn_a.turn_id] == ("claude_transcript", 200)
    assert usage[turn_b.turn_id][1] == 200


def test_a_turn_cut_off_by_a_restart_reconciles_by_hand(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from bobi.cli import metrics

    _transcript, turn_a = _crash_before_restart(monkeypatch, tmp_path, time.time_ns() // 1000 - _MINUTE_US)
    MetricsCollectorService(tmp_path).collect_once()
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)

    result = CliRunner().invoke(metrics, ["reconcile", "--turn-id", turn_a.turn_id, "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["forced"] is False
    assert _turn_usage(tmp_path)[turn_a.turn_id] == ("claude_transcript", 200)


def test_a_turn_whose_writer_looks_alive_reconciles_only_when_forced(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from bobi.cli import metrics
    from bobi.sdk import save_session_id

    started = time.time_ns() // 1000 - _MINUTE_US
    transcript = tmp_path / "stuck.jsonl"
    transcript.write_text("".join(_claude_call("stuck", f"msg-{index}", started + index * 1_000_000)
                                  for index in (1, 2)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    save_session_id("agent", "stuck", root=tmp_path)
    # The writer's identity cannot be told apart from this live process.
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        turn = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
        _flush(runtime)
        MetricsCollectorService(tmp_path).collect_once()
        runner = CliRunner()

        refused = runner.invoke(metrics, ["reconcile", "--turn-id", turn.turn_id])
        assert refused.exit_code == 1
        assert "still running" in refused.output and "--force" in refused.output
        assert runner.invoke(metrics, ["reconcile", "--force"]).exit_code == 2

        # Forcing skips the liveness check, never the import of what it spooled.
        runtime.begin_turn("other", provider="anthropic", brain="claude")
        _flush(runtime)
        pending = runner.invoke(metrics, ["reconcile", "--turn-id", turn.turn_id, "--force"])
        assert pending.exit_code == 1
        assert "not imported yet" in pending.output
        MetricsCollectorService(tmp_path).collect_once()

        forced = runner.invoke(metrics, ["reconcile", "--turn-id", turn.turn_id, "--force", "--wait", "--json"])
    finally:
        runtime.close()

    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.output)["forced"] is True
    assert _turn_usage(tmp_path)[turn.turn_id] == ("claude_transcript", 200)


def test_a_writer_that_ends_its_turn_and_exits_after_the_import_keeps_its_own_calls(monkeypatch, tmp_path):
    from bobi.brain.base import BrainToolExecution
    from bobi.sdk import save_session_id

    started = time.time_ns() // 1000 - _MINUTE_US
    transcript = tmp_path / "worker.jsonl"
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "worker", root=tmp_path)
    # The pid is already dead, so the worker reads as exited from the start.
    worker = _dead_runtime(monkeypatch, tmp_path)
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    turn = worker.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
    _flush(worker)
    imported = service._collect_once_locked()  # the cycle's import: the turn is open

    # Before the cycle's reconcile step the worker makes three calls with
    # three tools, ends its turn and exits.
    message_ids = [f"msg-{index}" for index in (1, 2, 3)]
    transcript.write_text("".join(_claude_call("worker", message_id, started + index * 1_000_000)
                                  for index, message_id in enumerate(message_ids, 1)))
    result = _claude_result("worker", message_ids)
    turn.record_result(replace(result, tool_executions=[
        BrainToolExecution(provider_tool_call_id=f"tool-{message_id}", tool_name="Bash",
                           triggering_provider_event_id=message_id)
        for message_id in message_ids]))
    turn.finish(status="completed")
    _flush(worker)
    worker.close()
    service._refresh_health(service._reconcile_locked(imported))
    service._cycle_locked()

    health = service.health()
    assert health["quarantined_events"] == 0
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_executions WHERE turn_id=?",
                            (turn.turn_id,)).fetchone()[0] == 3
    finally:
        conn.close()
    assert _turn_usage(tmp_path)[turn.turn_id][1] == 300


def test_a_dead_turn_stops_at_the_next_turn_of_its_resumed_provider_session(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    now = time.time_ns() // 1000
    a_start, b_start, c_start = now - 70 * _MINUTE_US, now - 30 * _MINUTE_US, now - 5 * _MINUTE_US
    calls = [("msg-a1", a_start + 10_000_000), ("msg-a2", a_start + 20_000_000),
             ("msg-b1", b_start + 10_000_000),
             ("msg-c1", c_start + 10_000_000), ("msg-c2", c_start + 20_000_000)]
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text("".join(_claude_call("shared", message_id, at) for message_id, at in calls))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "shared", root=tmp_path)

    # The first process dies mid-turn; a new one resumes the same provider session.
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn_a = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=a_start)
    _flush(crashed)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    try:
        turn_b = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        turn_b.record_result(_claude_result("shared", ["msg-b1"]))
        turn_b.finish(status="completed")
        turn_c = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=c_start)
        _flush(runtime)
        service._cycle_locked()
        assert _turn_usage(tmp_path)[turn_a.turn_id] == ("claude_transcript", 200)

        with transcript.open("a") as handle:
            handle.write(_claude_call("shared", "msg-c3", c_start + 30_000_000))
        turn_c.record_result(_claude_result("shared", ["msg-c1", "msg-c2", "msg-c3"]))
        turn_c.finish(status="completed")
        _flush(runtime)
        service._cycle_locked()
    finally:
        runtime.close()

    usage = _turn_usage(tmp_path)
    assert {turn: usage[turn][1] for turn in (turn_a.turn_id, turn_b.turn_id, turn_c.turn_id)} == {
        turn_a.turn_id: 200, turn_b.turn_id: 100, turn_c.turn_id: 300}


def test_a_dead_turn_stops_at_a_resumed_turn_that_is_still_running(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    now = time.time_ns() // 1000
    a_start, b_start = now - 70 * _MINUTE_US, now - 5 * _MINUTE_US
    calls = [("msg-a1", a_start + 10_000_000), ("msg-a2", a_start + 20_000_000),
             ("msg-b1", b_start + 10_000_000)]
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text("".join(_claude_call("shared", message_id, at) for message_id, at in calls))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "shared", root=tmp_path)

    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn_a = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=a_start)
    _flush(crashed)
    # The new process resumes the saved provider session; its first turn has no end yet.
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        turn_b = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        _flush(runtime)
        MetricsCollectorService(tmp_path, reconcile_interval=0)._cycle_locked()
    finally:
        runtime.close()

    usage = _turn_usage(tmp_path)
    assert usage[turn_a.turn_id] == ("claude_transcript", 200)
    assert turn_b.turn_id not in usage


def test_a_dead_turn_leaves_the_first_call_of_a_resumed_turn_to_it(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    now = time.time_ns() // 1000
    a_start, b_start = now - 70 * _MINUTE_US, now - _MINUTE_US
    calls = [("msg-a1", a_start + 10_000_000), ("msg-a2", a_start + 20_000_000),
             ("msg-b1", b_start + 3_000_000)]
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text("".join(_claude_call("shared", message_id, at) for message_id, at in calls))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "shared", root=tmp_path)
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn_a = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=a_start)
    _flush(crashed)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    try:
        turn_b = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        _flush(runtime)
        service._cycle_locked()
        with transcript.open("a") as handle:
            handle.write(_claude_call("shared", "msg-b2", b_start + 20_000_000))
        turn_b.record_result(_claude_result("shared", ["msg-b1", "msg-b2"]))
        turn_b.finish(status="completed")
        _flush(runtime)
        service._cycle_locked()
    finally:
        runtime.close()

    usage = _turn_usage(tmp_path)
    assert usage[turn_a.turn_id] == ("claude_transcript", 200)
    assert usage[turn_b.turn_id][1] == 200
    assert sum(output for _source, output in usage.values()) == 400


def test_a_dead_codex_turn_keeps_its_own_snapshot_not_the_next_turns(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    def line(at_us, payload, kind="event_msg"):
        at = datetime.fromtimestamp(at_us / 1_000_000, tz=timezone.utc)
        return json.dumps({"timestamp": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                           "type": kind, "payload": payload}) + "\n"

    def snapshot(at_us, input_tokens, output_tokens):
        return line(at_us, {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": output_tokens}}})

    now = time.time_ns() // 1000
    a_start, b_start = now - 70 * _MINUTE_US, now - _MINUTE_US
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        line(a_start, {"id": "thread", "cli_version": "0.144.5"}, kind="session_meta")
        + line(a_start + 1_000_000, {"type": "task_started", "turn_id": "t1"})
        + snapshot(a_start + 10_000_000, 1000, 100)
        + line(b_start + 1_000_000, {"type": "task_started", "turn_id": "t2"})
        + snapshot(b_start + 3_000_000, 1500, 150))
    monkeypatch.setattr("bobi.metrics.reconcile.find_codex_rollout", lambda _session_id: rollout)
    save_session_id("agent", "thread", root=tmp_path)
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn_a = crashed.begin_turn("agent", provider="openai", brain="codex", started_at_us=a_start)
    _flush(crashed)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        runtime.begin_turn("agent", provider="openai", brain="codex", started_at_us=b_start)
        _flush(runtime)
        MetricsCollectorService(tmp_path, reconcile_interval=0)._cycle_locked()
    finally:
        runtime.close()

    conn = connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        assert tuple(conn.execute(
            "SELECT measurement_source,input_tokens,output_tokens,provider_event_id FROM best_usage "
            "WHERE turn_id=? AND scope='turn'", (turn_a.turn_id,)).fetchone()) == ("codex_rollout", 1000, 100, "t1")
    finally:
        conn.close()


def test_a_turn_leaves_the_last_call_of_an_unreconciled_previous_turn_to_it(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    now = time.time_ns() // 1000
    a_start, b_start = now - 70 * _MINUTE_US, now - _MINUTE_US
    # A dies with its last call stamped one second before the resumed turn B starts.
    calls = [("msg-a1", a_start + 10_000_000), ("msg-a2", b_start - 1_000_000),
             ("msg-b1", b_start + 10_000_000)]
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text("".join(_claude_call("shared", message_id, at) for message_id, at in calls))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "shared", root=tmp_path)
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn_a = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=a_start)
    _flush(crashed)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        turn_b = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        turn_b.finish(status="completed")
        _flush(runtime)
    finally:
        runtime.close()
    MetricsCollectorService(tmp_path, reconcile_interval=0).collect_once()

    # B is reconciled first, while A has no recovery yet.
    reconcile_turn(tmp_path, turn_b.turn_id, wait=True)
    reconcile_turn(tmp_path, turn_a.turn_id, wait=True)

    usage = _turn_usage(tmp_path)
    assert usage[turn_b.turn_id] == ("claude_transcript", 100)
    assert usage[turn_a.turn_id] == ("claude_transcript", 200)


def test_a_late_transcript_line_stays_with_the_turn_whose_stream_recorded_it(monkeypatch, tmp_path):
    transcript = tmp_path / "shared.jsonl"
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        previous_start = time.time_ns() // 1000 - 10_000_000
        previous = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=previous_start)
        previous.record_result(_claude_result("shared", ["msg-p1", "msg-p2"]))
        previous.finish(status="completed")
        next_start = time.time_ns() // 1000 + 500_000
        following = runtime.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=next_start)
        following.record_result(_claude_result("shared", ["msg-n1"]))
        following.finish(status="completed")
        _flush(runtime)
    finally:
        runtime.close()
    # The previous turn's last line is stamped a second after the next turn started.
    transcript.write_text(_claude_call("shared", "msg-p1", previous_start + 1_000_000)
                          + _claude_call("shared", "msg-p2", next_start + 1_000_000)
                          + _claude_call("shared", "msg-n1", next_start + 3_000_000))
    MetricsCollectorService(tmp_path, reconcile_interval=0)._cycle_locked()

    conn = connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        transcript_rows = {row[0]: row[1] for row in conn.execute(
            "SELECT turn_id,output_tokens FROM usage_measurements "
            "WHERE scope='turn' AND measurement_source='claude_transcript'")}
    finally:
        conn.close()
    assert transcript_rows == {previous.turn_id: 200, following.turn_id: 100}


def test_turns_still_running_cannot_starve_a_newer_dead_turn(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        for index in range(4000):
            runtime.begin_turn(f"stuck-{index}", provider="anthropic", brain="claude")
        _flush(runtime)
    finally:
        runtime.close()
    started = time.time_ns() // 1000
    transcript = tmp_path / "crashed.jsonl"
    transcript.write_text(_claude_call("crashed", "msg-1", started + 1_000_000))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "crashed", root=tmp_path)
    crashed = _dead_runtime(monkeypatch, tmp_path)
    turn = crashed.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=started)
    _flush(crashed)
    MetricsCollectorService(tmp_path, reconcile_interval=0).collect_once()

    result = reconcile_missing(tmp_path, terminal_only=True, track_attempts=True, budget_seconds=0.5)

    assert [item["turn_id"] for item in result["results"]] == [turn.turn_id]


def test_a_live_turn_is_not_cut_short_by_another_live_process(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    now = time.time_ns() // 1000
    a_start, b_start = now - 10 * _MINUTE_US, now - _MINUTE_US
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text("".join(_claude_call("shared", f"msg-a{index}", a_start + index * 1_000_000)
                                  for index in (1, 2, 3)))
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: transcript)
    save_session_id("agent", "shared", root=tmp_path)
    first = MetricsRuntime(tmp_path, mode="enabled")
    second = MetricsRuntime(tmp_path, mode="enabled")
    service = MetricsCollectorService(tmp_path, reconcile_interval=0)
    try:
        turn_a = first.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=a_start)
        second.begin_turn("agent", provider="anthropic", brain="claude", started_at_us=b_start)
        _flush(first)
        _flush(second)
        service._cycle_locked()
        assert turn_a.turn_id not in _turn_usage(tmp_path)
        with pytest.raises(ReconciliationError, match="still running"):
            reconcile_turn(tmp_path, turn_a.turn_id)
    finally:
        first.close()
        second.close()


def test_an_upgraded_database_finds_a_turn_writer_through_an_index(tmp_path):
    from bobi.metrics.reconcile import _TURN_WRITER_SQL
    from bobi.metrics.schema import SCHEMA_SQL

    db = tmp_path / "metrics.db"
    previous = sqlite3.connect(db)
    previous.executescript("\n".join(
        line for line in SCHEMA_SQL.splitlines() if "idx_raw_turn_writer" not in line))
    previous.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')")
    previous.commit()
    previous.close()

    conn = connect(db)
    try:
        migrate(conn)
        plan = " ".join(str(row[3]) for row in conn.execute(
            f"EXPLAIN QUERY PLAN {_TURN_WRITER_SQL}", ("turn-1",)))
    finally:
        conn.close()
    assert "idx_raw_turn_writer" in plan


def test_cli_reports_an_unsupported_codex_rollout_as_an_error(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from bobi.cli import metrics

    _seed_turn(tmp_path, provider="openai", brain="codex", provider_session_id="fixture",
               turn_id="turn-codex", estimate=None)
    rollout = _rollout(
        tmp_path,
        {"type": "session_meta", "payload": {"id": "fixture", "cli_version": "9.0.0"}},
        _event({"type": "task_started", "turn_id": "t1"}),
        _event({"type": "token_count", "info": {"total_token_usage": {"input": 13364, "cached": 8960, "output": 5}}}),
        _event({"type": "task_complete", "turn_id": "t1"}),
    )
    monkeypatch.setattr("bobi.metrics.reconcile.find_codex_rollout", lambda _session_id: rollout)
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)

    result = CliRunner().invoke(metrics, ["reconcile", "--turn-id", "turn-codex"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Error: " in result.output and "no usage in a known shape" in result.output


def test_scheduled_reconciliation_skips_active_turns(tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    from bobi.metrics.store import migrate

    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,"
        "started_at_us,status) VALUES(?,?,?,?,?,?)",
        ("session-1", "agent", "claude", "anthropic", 1, "running"),
    )
    for turn_id, ended_at_us, status in (
        ("turn-active", None, "running"),
        ("turn-complete", 4, "completed"),
    ):
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,"
            "is_user_initiated,started_at_us,ended_at_us,status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                turn_id,
                "session-1",
                1 if turn_id == "turn-active" else 2,
                "test",
                1,
                time.time_ns() // 1000 if turn_id == "turn-active" else 3,
                ended_at_us,
                status,
            ),
        )
    conn.commit()
    conn.close()

    result = reconcile_missing(tmp_path, terminal_only=True)

    # The finished turn is attempted (and fails: it has no provider session);
    # the active one is skipped without counting as a failure.
    assert [failure["turn_id"] for failure in result["failures"]] == ["turn-complete"]
    assert result["turns_considered"] == 1


@pytest.mark.parametrize("provider,brain", [("anthropic", "claude"), ("gateway", "claude"), ("gateway", "gateway")])
def test_claude_reconciliation_is_idempotent_and_supersedes_estimate(metrics_fixtures,
    monkeypatch, tmp_path, provider, brain
):
    root = tmp_path / "agent"
    turn_id = "turn-claude"
    estimate = {
        "measurement_id": "estimate-claude",
        "scope": "turn",
        "turn_id": turn_id,
        "invocation_id": None,
        "provider": provider,
        "model": "claude-opus-4-8",
        "provider_event_id": None,
        "measurement_source": "calibrated_estimator",
        "is_estimated": 1,
        "estimator_name": "fixture",
        "estimator_version": "1",
        "token_semantics_version": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "observed_at_us": _us("2026-09-25T00:00:00Z"),
    }
    _seed_turn(
        root,
        provider=provider,
        brain=brain,
        provider_session_id="fixture-claude-session",
        turn_id=turn_id,
        estimate=estimate,
    )
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_claude_transcript",
        lambda _session_id: metrics_fixtures / "claude-transcript.jsonl",
    )
    monkeypatch.setattr("bobi.metrics.reconcile.find_codex_rollout", lambda _session_id: None)

    first = reconcile_turn(root, turn_id, wait=True)
    second = reconcile_turn(root, turn_id, wait=True)

    assert first["status"] == "done"
    assert first["exact_measurements_recovered"] == 4
    assert second["exact_measurements_recovered"] == 0
    db = root / "state" / "metrics" / "metrics.db"
    conn = connect(db, readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0] == 5
        exact = conn.execute(
            "SELECT * FROM best_usage WHERE turn_id=? AND scope='turn' "
            "AND model='claude-opus-4-8'",
            (turn_id,),
        ).fetchone()
        assert exact["is_estimated"] == 0
        assert exact["provider"] == provider
        assert first["measurement_source"] == "claude_transcript"
        assert second["exact_measurements_recovered"] == 0
        assert exact["measurement_source"] == "claude_transcript"
        assert exact["supersedes_measurement_id"] == "estimate-claude"
        assert exact["cache_write_1h_input_tokens"] == 8192
    finally:
        conn.close()


@pytest.mark.parametrize("provider,brain", [("openai", "codex"), ("gateway", "codex"), ("gateway", "gateway")])
def test_codex_reconciliation_uses_final_turn_usage(metrics_fixtures, monkeypatch, tmp_path, provider, brain):
    root = tmp_path / "agent"
    turn_id = "turn-codex"
    estimate = {
        "measurement_id": "estimate-codex",
        "scope": "turn",
        "turn_id": turn_id,
        "invocation_id": None,
        "provider": provider,
        "model": "gpt-test",
        "provider_event_id": None,
        "measurement_source": "local_tokenizer",
        "is_estimated": 1,
        "estimator_name": "fixture",
        "estimator_version": "1",
        "token_semantics_version": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "observed_at_us": _us("2026-09-25T00:00:00Z"),
    }
    _seed_turn(
        root,
        provider=provider,
        brain=brain,
        provider_session_id="fixture-codex-thread",
        turn_id=turn_id,
        estimate=estimate,
    )
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_codex_rollout",
        lambda _session_id: metrics_fixtures / "codex-rollout.jsonl",
    )
    monkeypatch.setattr("bobi.metrics.reconcile.find_claude_transcript", lambda _session_id: None)

    result = reconcile_turn(root, turn_id, wait=True)

    assert result["exact_measurements_recovered"] == 1
    conn = connect(root / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        exact = conn.execute(
            "SELECT * FROM best_usage WHERE turn_id=? AND scope='turn'",
            (turn_id,),
        ).fetchone()
        assert exact["measurement_source"] == "codex_rollout"
        assert exact["provider"] == provider
        assert exact["input_tokens"] == 4096
        assert exact["cache_read_input_tokens"] == 1024
        assert exact["reasoning_output_tokens"] == 12
        assert exact["supersedes_measurement_id"] == "estimate-codex"
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_invocations WHERE turn_id=?",
            (turn_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_codex_reconciliation_backfills_uncorrelated_online_invocation(metrics_fixtures,
    monkeypatch, tmp_path
):
    root = tmp_path / "agent"
    turn_id = "turn-codex-online"
    _seed_turn(
        root,
        provider="openai",
        brain="codex",
        provider_session_id="fixture-codex-thread",
        turn_id=turn_id,
        estimate=None,
    )
    segment = root / "state" / "metrics" / "spool" / "online" / "one.telemetry"
    with SpoolWriter(segment) as writer:
        writer.append(MetricsEvent(
            event_type="invocation.recorded",
            producer_id="online-fixture",
            producer_sequence=1,
            source="test",
            payload={
                "invocation_id": "online-invocation",
                "turn_id": turn_id,
                "invocation_index": 1,
                "provider": "openai",
                "model_selected": "gpt-test",
                "provider_event_id": None,
                "started_at_us": _us("2026-09-25T00:00:00Z"),
                "ended_at_us": _us("2026-09-25T00:00:01Z"),
                "status": "completed",
            },
            turn_id=turn_id,
            invocation_id="online-invocation",
        ))
    db = root / "state" / "metrics" / "metrics.db"
    MetricsCollector(db).collect_segment(segment)
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_codex_rollout",
        lambda _session_id: metrics_fixtures / "codex-rollout.jsonl",
    )

    result = reconcile_turn(root, turn_id, wait=True)

    assert result["status"] == "done"
    conn = connect(db, readonly=True)
    try:
        invocation = conn.execute(
            "SELECT invocation_id,invocation_index,provider_event_id "
            "FROM llm_invocations WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        assert tuple(invocation) == (
            "online-invocation", 1, "resp-fixture-final"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_events WHERE projection_state='quarantined'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_reconciliation_recovers_provider_session_id_from_runtime_state(metrics_fixtures,
    monkeypatch, tmp_path
):
    root = tmp_path / "agent"
    turn_id = "turn-killed"
    _seed_turn(
        root,
        provider="anthropic",
        brain="claude",
        provider_session_id=None,
        turn_id=turn_id,
        estimate=None,
    )
    sessions = root / "state" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "agent.id").write_text("fixture-claude-session")
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_claude_transcript",
        lambda _session_id: metrics_fixtures / "claude-transcript.jsonl",
    )

    result = reconcile_turn(root, turn_id, wait=True)

    assert result["status"] == "done"
    conn = connect(root / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        assert conn.execute(
            "SELECT provider_session_id FROM sessions"
        ).fetchone()[0] == "fixture-claude-session"
    finally:
        conn.close()


def test_reconciliation_reserves_stable_sequences_with_partial_invocations(metrics_fixtures,
    monkeypatch, tmp_path
):
    root = tmp_path / "agent"
    turn_id = "turn-partial"
    _seed_turn(
        root,
        provider="anthropic",
        brain="claude",
        provider_session_id="fixture-claude-session",
        turn_id=turn_id,
        estimate=None,
    )
    segment = root / "state" / "metrics" / "spool" / "partial" / "one.telemetry"
    with SpoolWriter(segment) as writer:
        writer.append(MetricsEvent(
            event_type="invocation.recorded",
            producer_id="partial-fixture",
            producer_sequence=1,
            source="test",
            payload={
                "invocation_id": "existing-invocation",
                "turn_id": turn_id,
                "invocation_index": 1,
                "provider": "anthropic",
                "model_selected": "claude-opus-4-8",
                "provider_event_id": "msg-fixture-1",
                "started_at_us": _us("2026-09-25T00:00:00Z"),
                "ended_at_us": _us("2026-09-25T00:00:01Z"),
                "status": "completed",
            },
            turn_id=turn_id,
            invocation_id="existing-invocation",
        ))
    MetricsCollector(root / "state" / "metrics" / "metrics.db").collect_segment(
        segment
    )
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_claude_transcript",
        lambda _session_id: metrics_fixtures / "claude-transcript.jsonl",
    )

    first = reconcile_turn(root, turn_id, wait=True)
    second = reconcile_turn(root, turn_id, wait=True)

    assert first["exact_measurements_recovered"] == 4
    assert second["exact_measurements_recovered"] == 0
    conn = connect(root / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        rows = conn.execute(
            "SELECT invocation_index,provider_event_id FROM llm_invocations "
            "WHERE turn_id=? ORDER BY invocation_index",
            (turn_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "msg-fixture-1"),
            (2, "msg-fixture-sidechain"),
        ]
        sequences = conn.execute(
            "SELECT producer_sequence,event_type FROM raw_events "
            "WHERE producer_id=? ORDER BY producer_sequence",
            (f"reconciler-anthropic-{turn_id}",),
        ).fetchall()
        assert len({row[0] for row in sequences}) == len(sequences)
    finally:
        conn.close()


def test_default_spawn_marks_supervisor_as_metrics_collector_owner(
    monkeypatch, tmp_path
):
    captured = {}

    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("bobi.supervisor.supervision.subprocess.Popen", popen)
    supervisor = Supervisor([], SupervisorConfig(), project_root=tmp_path,
                            collector_owner=True)

    supervisor._default_spawn()

    assert captured["env"]["BOBI_METRICS_COLLECTOR_OWNER"] == "supervisor"


def test_manager_owns_a_fail_safe_metrics_collector_in_shadow_mode(
    bobi_install, monkeypatch, tmp_path
):
    import signal

    from bobi.config import Config
    from bobi.service import run_manager_from_config

    calls = []

    class Collector:
        def __init__(self, root):
            calls.append(("init", root))

        def start(self):
            calls.append(("start",))

        def stop(self, *, timeout):
            calls.append(("stop", timeout))
            return True

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")
    monkeypatch.setattr("bobi.metrics.collector.MetricsCollectorService", Collector)
    monkeypatch.setattr("bobi.manager_health.start", lambda *args, **kwargs: 1)
    monkeypatch.setattr("bobi.subagent.run_persistent_agent", lambda **kwargs: None)
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        run_manager_from_config(
            bobi_install.repo_path, Config.load(bobi_install.repo_path)
        )
    finally:
        signal.signal(signal.SIGTERM, previous_term)

    assert calls == [
        ("init", bobi_install.repo_path),
        ("start",),
        ("stop", 2),
    ]


def test_supervised_manager_does_not_start_a_second_metrics_collector(
    bobi_install, monkeypatch, tmp_path
):
    import signal

    from bobi.config import Config
    from bobi.service import run_manager_from_config

    calls = []

    class Collector:
        def __init__(self, root):
            calls.append(("init", root))

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")
    monkeypatch.setenv("BOBI_METRICS_COLLECTOR_OWNER", "supervisor")
    monkeypatch.setattr("bobi.metrics.collector.MetricsCollectorService", Collector)
    monkeypatch.setattr("bobi.manager_health.start", lambda *args, **kwargs: 1)
    monkeypatch.setattr("bobi.subagent.run_persistent_agent", lambda **kwargs: None)
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        run_manager_from_config(
            bobi_install.repo_path, Config.load(bobi_install.repo_path)
        )
    finally:
        signal.signal(signal.SIGTERM, previous_term)

    assert calls == []
    # Only this manager defers to the supervisor; nothing it spawns inherits that.
    assert "BOBI_METRICS_COLLECTOR_OWNER" not in os.environ


def test_read_model_files_are_as_private_as_the_spool(tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    previous = os.umask(0o022)
    try:
        conn = connect(db)
        migrate(conn)
        conn.commit()
        modes = {path.name: path.stat().st_mode & 0o777 for path in db.parent.iterdir()}
        conn.close()
    finally:
        os.umask(previous)
    # The database copies every spool payload, so it gets the segments' 0600.
    assert modes["metrics.db"] == 0o600
    assert modes["metrics.db-wal"] == 0o600 and modes["metrics.db-shm"] == 0o600


def test_an_existing_world_readable_read_model_is_tightened(tmp_path):
    db = tmp_path / "metrics.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    connect(db).close()
    assert db.stat().st_mode & 0o777 == 0o600
