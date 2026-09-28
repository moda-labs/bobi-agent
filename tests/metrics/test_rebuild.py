import pytest

from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent
from bobi.metrics.rebuild import rebuild_metrics
from bobi.metrics.spool import HEADER, SpoolWriter
from bobi.metrics.store import connect, logical_snapshot
from bobi.metrics.maintenance import prune_result_files


def _seed(root):
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
                "brain": "stub",
                "provider": "stub",
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
                "started_at_us": 2,
                "ended_at_us": 3,
                "status": "completed",
            },
        ),
    ]
    with SpoolWriter(segment) as writer:
        for event in events:
            writer.append(event)
    db = root / "state" / "metrics" / "metrics.db"
    MetricsCollector(db).collect_segment(segment)
    return db


def test_rebuild_preserves_forensics_and_activates_matching_database(tmp_path):
    db = _seed(tmp_path)
    conn = connect(db, readonly=True)
    try:
        before = logical_snapshot(conn)
    finally:
        conn.close()

    result = rebuild_metrics(tmp_path)

    assert result["status"] == "done"
    assert result["source"] == "raw_events"
    assert result["snapshot"] == before
    forensic = __import__("pathlib").Path(result["forensic_directory"])
    assert (forensic / "metrics.db").exists()
    assert __import__("pathlib").Path(result["backup"]["database"]).exists()
    conn = connect(db, readonly=True)
    try:
        assert logical_snapshot(conn) == before
    finally:
        conn.close()


def test_rebuild_falls_back_to_retained_segments_when_source_projection_fails(
    monkeypatch, tmp_path
):
    db = _seed(tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.rebuild.rebuild_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt")),
    )

    result = rebuild_metrics(tmp_path)

    assert result["status"] == "done"
    assert result["source"] == "retained_segments"
    assert result["segments_scanned"] == 1
    conn = connect(db, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    finally:
        conn.close()


def test_rebuild_refuses_partial_activation_from_corrupt_segments(
    monkeypatch, tmp_path
):
    db = _seed(tmp_path)
    conn = connect(db, readonly=True)
    try:
        before = logical_snapshot(conn)
    finally:
        conn.close()
    segment = next((tmp_path / "state" / "metrics" / "spool").rglob("*.telemetry"))
    damaged = bytearray(segment.read_bytes())
    damaged[HEADER.size] ^= 1
    segment.write_bytes(damaged)
    monkeypatch.setattr(
        "bobi.metrics.rebuild.rebuild_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt")),
    )

    with pytest.raises(RuntimeError, match="corrupt spool segment"):
        rebuild_metrics(tmp_path)

    conn = connect(db, readonly=True)
    try:
        assert logical_snapshot(conn) == before
    finally:
        conn.close()


def test_rebuild_activation_failure_restores_checkpointed_source(
    monkeypatch, tmp_path
):
    db = _seed(tmp_path)
    conn = connect(db, readonly=True)
    try:
        before = logical_snapshot(conn)
    finally:
        conn.close()

    from bobi.metrics import rebuild

    preserve = rebuild._preserve_forensics
    real_integrity_check = rebuild.integrity_check
    checks = 0

    def corrupt_forensic(metrics_root):
        forensic = preserve(metrics_root)
        (forensic / "metrics.db").write_bytes(b"not a sqlite database")
        return forensic

    def fail_active_verification(conn):
        nonlocal checks
        checks += 1
        if checks == 3:
            return "corrupt"
        return real_integrity_check(conn)

    monkeypatch.setattr(rebuild, "_preserve_forensics", corrupt_forensic)
    monkeypatch.setattr(rebuild, "integrity_check", fail_active_verification)

    with pytest.raises(
        RuntimeError, match="activated metrics database verification failed"
    ):
        rebuild_metrics(tmp_path)

    conn = connect(db, readonly=True)
    try:
        assert real_integrity_check(conn) == "ok"
        assert logical_snapshot(conn) == before
    finally:
        conn.close()
    assert not list(db.parent.glob(".metrics-rollback-*.db"))


def test_maintenance_results_are_bounded(tmp_path):
    root = tmp_path / "state" / "metrics" / "maintenance"
    root.mkdir(parents=True)
    for index in range(5):
        (root / f"{index}.result.json").write_text("{}\n")

    removed = prune_result_files(tmp_path, keep=2, max_age_days=30)

    assert removed == 3
    assert len(list(root.glob("*.result.json"))) == 2
