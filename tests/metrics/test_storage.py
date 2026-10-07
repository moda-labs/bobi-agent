"""Durable ingestion, SQLite integrity, and recovery regressions."""

import json
import pytest
import sqlite3
import threading
import time
import uuid
from bobi.brain.base import BrainUsage, TurnResult
from bobi.fsutil import try_file_lock
from bobi.metrics.collector import MetricsCollector, MetricsCollectorService
from bobi.metrics.estimate import (
    CalibrationSample,
    EstimatorQualification,
    EstimatorRegistry,
    calibrate_estimators,
    estimate_missing,
    estimate_turn,
    qualify_byte_estimator,
)
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
from bobi.metrics.store import connect, integrity_check, migrate, stage_frames
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


def test_scheduled_reconciliation_collects_exact_before_estimation(
    monkeypatch, tmp_path
):
    import bobi.metrics.estimate as estimate_module
    import bobi.metrics.reconcile as reconcile_module

    service = MetricsCollectorService(tmp_path)
    order = []
    monkeypatch.setattr(
        reconcile_module,
        "reconcile_missing",
        lambda root, collect, terminal_only, retry_attempts: (
            order.append(("reconcile", terminal_only)) or {"errors": 0}
        ),
    )
    monkeypatch.setattr(
        estimate_module,
        "estimate_missing",
        lambda root, collect, terminal_only: (
            order.append(("estimate", terminal_only)) or {}
        ),
    )
    monkeypatch.setattr(
        service,
        "_collect_once_locked",
        lambda: order.append("collect") or service._empty_result("active"),
    )

    service._reconcile_locked(service._empty_result("active"))

    assert order == [
        ("reconcile", True),
        "collect",
        ("estimate", True),
        "collect",
    ]
    assert service.health()["reconciliation_errors"] == 0
    assert service.health()["last_reconciliation_at_us"] is not None


def test_scheduled_reconciliation_failures_are_health_only(monkeypatch, tmp_path):
    import bobi.metrics.estimate as estimate_module
    import bobi.metrics.reconcile as reconcile_module

    service = MetricsCollectorService(tmp_path)
    service._set_health(reconciliation_errors=4)
    monkeypatch.setattr(
        reconcile_module,
        "reconcile_missing",
        lambda root, collect, terminal_only, retry_attempts: (
            _ for _ in ()
        ).throw(RuntimeError("reconcile")),
    )
    monkeypatch.setattr(
        estimate_module,
        "estimate_missing",
        lambda root, collect, terminal_only: (
            _ for _ in ()
        ).throw(RuntimeError("estimate")),
    )
    monkeypatch.setattr(
        service, "_collect_once_locked", lambda: service._empty_result("active")
    )

    result = service._reconcile_locked(service._empty_result("active"))

    assert result["role"] == "active"
    assert service.health()["reconciliation_errors"] == 6
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
    with pytest.raises(ValueError, match="deduplication key"):
        stage_frames(
            conn,
            segment_path=second_segment,
            frames=iter_frames(second_segment),
        )
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

    with SpoolWriter(segment) as writer:
        writer.append(_projection_event("experiment_outcome.recorded", 3, {
            "outcome_id": "historical-outcome",
            "router_decision_id": decision["router_decision_id"],
            "outcome_name": "completion", "outcome_value": 1,
            "outcome_definition_version": "bobi-runtime-v1",
            "outcome_source": "runtime", "evaluator_name": "bobi-runtime",
            "evaluator_version": "1", "is_estimated": 0, "observed_at_us": 3,
        }, session_id="historical-session", turn_id="historical-turn"))
    assert MetricsCollector(database).collect_segment(segment)["projected"] == 1
    assert MetricsCollector(database).collect_segment(segment)["staged"] == 0
    conn = connect(database, readonly=True)
    try:
        assert conn.execute("SELECT outcome_value FROM experiment_outcomes").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
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


def test_experiment_configuration_is_immutable_per_experiment_id(tmp_path):
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
        assert result["quarantined"] == 1
        assert conn.execute("SELECT COUNT(*) FROM router_decisions").fetchone()[0] == 1
        error = conn.execute(
            "SELECT projection_error FROM raw_events WHERE event_type='router_decision.recorded' "
            "AND projection_state='quarantined'"
        ).fetchone()[0]
        assert "experiment configuration changed" in error
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
    monkeypatch.setattr("bobi.metrics.estimate.estimate_missing", lambda *args, **kwargs: {})
    for index in range(5):
        service = MetricsCollectorService(tmp_path, reconcile_interval=0)
        service._cycle_locked()
        service._publish_health(force=True)
    assert calls == ["missing-turn"] * 3
    assert service.health()["reconciliation_retry_exhausted_turns"] == 1
    assert service.health()["reconciliation_attempts"] == {"missing-turn": 3}
    assert reconcile_missing(tmp_path)["errors"] == 1
    assert len(calls) == 4


def test_standby_collector_preserves_retry_limit_on_takeover(monkeypatch, tmp_path):
    from bobi.metrics.collector import MetricsCollectorService

    _seed_turn(tmp_path, provider="gateway", brain="claude", provider_session_id="missing-session",
               turn_id="missing-turn", estimate=None)
    calls = []
    def unavailable(root, turn_id, **kwargs):
        calls.append(turn_id)
        raise ReconciliationError("transcript not available")
    monkeypatch.setattr("bobi.metrics.reconcile.reconcile_turn", unavailable)
    monkeypatch.setattr("bobi.metrics.estimate.estimate_missing", lambda *args, **kwargs: {})
    first = MetricsCollectorService(tmp_path, poll_interval=0.01, reconcile_interval=0)
    second = MetricsCollectorService(tmp_path, poll_interval=0.01, reconcile_interval=0)
    try:
        first.start()
        deadline = time.monotonic() + 2
        while first.health()["reconciliation_attempts"].get("missing-turn", 0) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 3
        first._publish_health(force=True)
        second.start()
        deadline = time.monotonic() + 2
        while second.health()["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert second.health()["role"] == "standby"
        assert json.loads(first.health_path.read_text())["reconciliation_attempts"] == {"missing-turn": 3}
        assert first.stop(timeout=2)
        deadline = time.monotonic() + 2
        while second.health()["role"] != "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert second.health()["role"] == "active"
        assert second.health()["reconciliation_attempts"] == {"missing-turn": 3}
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


def test_codex_rollout_parser_rejects_unknown_versions(metrics_fixtures, tmp_path):
    source = metrics_fixtures / "codex-rollout.jsonl"
    target = tmp_path / "rollout.jsonl"
    lines = source.read_text().splitlines()
    first = json.loads(lines[0])
    first["payload"]["cli_version"] = "9.0.0"
    lines[0] = json.dumps(first)
    target.write_text("\n".join(lines) + "\n")

    with pytest.raises(ProviderContractError, match="unsupported codex rollout"):
        parse_codex_rollout_records(target)


def test_scheduled_reconciliation_skips_active_turns(monkeypatch, tmp_path):
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
                2 if turn_id == "turn-active" else 3,
                ended_at_us,
                status,
            ),
        )
    conn.commit()
    conn.close()
    seen = []
    monkeypatch.setattr(
        "bobi.metrics.reconcile.reconcile_turn",
        lambda root, turn_id, **kwargs: seen.append(turn_id) or {
            "exact_measurements_recovered": 0
        },
    )

    result = reconcile_missing(tmp_path, terminal_only=True)

    assert seen == ["turn-complete"]
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


def _seed_uncovered_turn(root, *, prompt_bytes=400, model="claude-test"):
    segment = root / "state" / "metrics" / "spool" / "fixture" / "one.telemetry"
    events = [
        MetricsEvent(
            event_type="session.recorded",
            producer_id="fixture",
            producer_sequence=0,
            source="test",
            session_id="session-1",
            payload={
                "session_id": "session-1",
                "session_name": "agent",
                "brain": "claude",
                "provider": "anthropic",
                "started_at_us": 1,
                "status": "completed",
            },
        ),
        MetricsEvent(
            event_type="turn.recorded",
            producer_id="fixture",
            producer_sequence=1,
            source="test",
            session_id="session-1",
            turn_id="turn-1",
            payload={
                "turn_id": "turn-1",
                "session_id": "session-1",
                "turn_index": 1,
                "trigger_kind": "test",
                "is_user_initiated": 1,
                "prompt_bytes": prompt_bytes,
                "started_at_us": 2,
                "ended_at_us": 3,
                "status": "completed",
            },
        ),
        MetricsEvent(
            event_type="invocation.recorded",
            producer_id="fixture",
            producer_sequence=2,
            source="test",
            session_id="session-1",
            turn_id="turn-1",
            invocation_id="inv-1",
            payload={
                "invocation_id": "inv-1",
                "turn_id": "turn-1",
                "invocation_index": 1,
                "provider": "anthropic",
                "model_selected": model,
                "started_at_us": 2,
                "ended_at_us": 3,
                "status": "completed",
            },
        ),
    ]
    with SpoolWriter(segment) as writer:
        for event in events:
            writer.append(event)
    MetricsCollector(root / "state" / "metrics" / "metrics.db").collect_segment(
        segment
    )


def test_byte_estimator_requires_one_hundred_held_out_samples():
    report = qualify_byte_estimator(
        provider="anthropic",
        model_family="claude-",
        calibration=[CalibrationSample(400, 100)] * 50,
        held_out=[CalibrationSample(400, 100)] * 99,
        estimator_version="1",
    )

    assert report.qualified is False
    assert report.mape_percent == 0


def test_byte_estimator_enforces_accuracy_gates():
    report = qualify_byte_estimator(
        provider="anthropic",
        model_family="claude-",
        calibration=[CalibrationSample(400, 100)] * 50,
        held_out=[CalibrationSample(400, 150)] * 100,
        estimator_version="1",
    )

    assert report.qualified is False
    assert report.mape_percent > 10
    assert report.p95_ape_percent > 20


def test_qualified_estimator_emits_nullable_cache_dimensions(tmp_path):
    _seed_uncovered_turn(tmp_path)
    qualification = EstimatorQualification(
        provider="anthropic",
        model_family="claude-",
        estimator_name="calibrated_bytes",
        estimator_version="fixture-1",
        tokens_per_byte=0.25,
        calibration_samples=100,
        held_out_samples=100,
        mape_percent=0,
        p95_ape_percent=0,
        qualified=True,
    )

    first = estimate_turn(
        tmp_path,
        "turn-1",
        registry=EstimatorRegistry([qualification]),
    )
    second = estimate_turn(
        tmp_path,
        "turn-1",
        registry=EstimatorRegistry([qualification]),
    )

    assert first["status"] == "done"
    assert second["status"] == "skipped"
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        row = conn.execute(
            "SELECT * FROM usage_measurements WHERE turn_id='turn-1'"
        ).fetchone()
        assert row["is_estimated"] == 1
        assert row["input_tokens"] == 100
        assert row["output_tokens"] is None
        assert row["cache_read_input_tokens"] is None
        assert row["cache_write_input_tokens"] is None
    finally:
        conn.close()


def test_unqualified_registry_produces_no_guess(tmp_path):
    _seed_uncovered_turn(tmp_path)

    result = estimate_turn(tmp_path, "turn-1", registry=EstimatorRegistry())

    assert result["status"] == "skipped"
    conn = connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_scheduled_estimation_skips_active_turns(monkeypatch, tmp_path):
    _seed_uncovered_turn(tmp_path)
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    conn.execute(
        "UPDATE turns SET ended_at_us=NULL,status='running' WHERE turn_id='turn-1'"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "bobi.metrics.estimate.estimate_turn",
        lambda *args, **kwargs: pytest.fail("active turn was estimated"),
    )

    result = estimate_missing(
        tmp_path, registry=EstimatorRegistry(), terminal_only=True
    )

    assert result["turns_considered"] == 0


def test_database_calibration_writes_report_and_qualified_registry(tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,"
        "started_at_us,status) VALUES(?,?,?,?,?,?)",
        ("session-1", "agent", "claude", "anthropic", 1, "completed"),
    )
    for index in range(200):
        turn_id = f"turn-{index}"
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,"
            "is_user_initiated,prompt_bytes,started_at_us,ended_at_us,status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (turn_id, "session-1", index + 1, "test", 1, 400, index + 2, index + 3, "completed"),
        )
        conn.execute(
            "INSERT INTO usage_measurements(measurement_id,scope,turn_id,"
            "provider,model,measurement_source,is_estimated,token_semantics_version,"
            "input_tokens,cache_write_breakdown_complete,observed_at_us) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"usage-{index}", "turn", turn_id, "anthropic", "claude-test", "provider_stream", 0, 1, 100, 0, index + 3),
        )
    conn.commit()
    conn.close()

    report = calibrate_estimators(tmp_path)

    assert report["exact_turns_considered"] == 200
    assert report["qualified_model_groups"] == 1
    assert report["estimators"][0]["held_out_samples"] == 100
    registry = json.loads(
        (tmp_path / "state" / "metrics" / "estimators.json").read_text()
    )
    assert len(registry["estimators"]) == 1


def test_default_spawn_marks_supervisor_as_metrics_collector_owner(
    monkeypatch, tmp_path
):
    captured = {}

    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("bobi.supervisor.supervision.subprocess.Popen", popen)
    supervisor = Supervisor([], SupervisorConfig(), project_root=tmp_path)

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
