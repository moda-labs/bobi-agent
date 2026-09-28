import time
import sqlite3
import json

from bobi.brain.base import BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.events import MetricsEvent
from bobi.metrics.maintenance import run_maintenance
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.spool import HEADER, SpoolWriter
from bobi.metrics.store import connect


def test_service_imports_spools_and_exposes_health(tmp_path):
    service = MetricsCollectorService(tmp_path, poll_interval=0.01)
    service.start()
    runtime = MetricsRuntime(tmp_path, mode="shadow")
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
                    if conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1:
                        break
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
        time.sleep(0.01)
    else:
        raise AssertionError("collector did not project the turn")

    health = service.health()
    assert health["status"] == "running"
    assert health["role"] == "active"
    assert health["db_ready"] is True
    assert health["producer_count"] == 1
    assert health["telemetry_events_dropped"] == 0
    assert health["uncommitted_spool_bytes"] == 0
    assert health["import_lag_ms"] == 0
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
        lambda root, collect: order.append("reconcile") or {"errors": 0},
    )
    monkeypatch.setattr(
        estimate_module,
        "estimate_missing",
        lambda root, collect: order.append("estimate") or {},
    )
    monkeypatch.setattr(
        service,
        "_collect_once_locked",
        lambda: order.append("collect") or service._empty_result("active"),
    )

    service._reconcile_locked(service._empty_result("active"))

    assert order == ["reconcile", "collect", "estimate", "collect"]
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
        lambda root, collect: (_ for _ in ()).throw(RuntimeError("reconcile")),
    )
    monkeypatch.setattr(
        estimate_module,
        "estimate_missing",
        lambda root, collect: (_ for _ in ()).throw(RuntimeError("estimate")),
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


def test_active_collector_executes_queued_rebuild(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="shadow")
    observation = runtime.begin_turn("agent", provider="openai", brain="codex")
    observation.finish(status="completed")
    runtime.close()
    service = MetricsCollectorService(tmp_path, poll_interval=0.01)
    service.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if service.health()["role"] == "active" and service.db_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("collector did not become active")

    result = run_maintenance(
        tmp_path, "rebuild", wait=True, timeout=5
    )

    assert result["status"] == "done"
    assert result["result"]["status"] == "done"
    assert service.stop(timeout=2)
