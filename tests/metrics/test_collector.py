import time
import sqlite3
import json

from bobi.brain.base import BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.runtime import MetricsRuntime
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
