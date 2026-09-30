import os
import time
import json
from collections import namedtuple

from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent
from bobi.metrics.retention import DAY_US, apply_retention, plan_retention
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect


def _session_event(producer, sequence, session_id, started):
    return MetricsEvent(
        event_type="session.recorded",
        producer_id=producer,
        producer_sequence=sequence,
        source="test",
        session_id=session_id,
        payload={
            "session_id": session_id,
            "session_name": session_id,
            "brain": "stub",
            "provider": "stub",
            "started_at_us": started,
            "ended_at_us": started + 1,
            "status": "completed",
        },
    )


def _seed(root, now_us):
    spool = root / "state" / "metrics" / "spool" / "producer"
    old = spool / "one.telemetry"
    current = spool / "two.telemetry"
    with SpoolWriter(old) as writer:
        writer.append(_session_event("producer", 0, "old-session", now_us - 40 * DAY_US))
    with SpoolWriter(current) as writer:
        writer.append(_session_event("producer", 1, "new-session", now_us))
    db = root / "state" / "metrics" / "metrics.db"
    collector = MetricsCollector(db)
    collector.collect_segment(old)
    collector.collect_segment(current)
    old_time = (now_us - 40 * DAY_US) / 1_000_000
    os.utime(old, (old_time, old_time))
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE raw_events SET received_at_us=? WHERE session_id='old-session'",
            (now_us - 40 * DAY_US,),
        )
        conn.commit()
    finally:
        conn.close()
    return db, old, current


def test_retention_dry_run_is_blocked_without_verified_backup(tmp_path):
    now_us = time.time_ns() // 1000
    _db, old, _current = _seed(tmp_path, now_us)

    plan = plan_retention(tmp_path, now_us=now_us)

    assert plan["status"] == "blocked"
    assert plan["retention_blocked"] is True
    assert [item["path"] for item in plan["archive_segments"]] == [str(old)]


def test_retention_apply_backs_up_archives_and_prunes(tmp_path):
    now_us = time.time_ns() // 1000
    db, old, current = _seed(tmp_path, now_us)

    result = apply_retention(tmp_path, now_us=now_us)

    assert result["status"] == "done"
    assert result["applied"] is True
    assert not old.exists()
    assert current.exists()
    assert len(result["archived_segments"]) == 1
    assert result["archived_segments"][0].endswith(".telemetry.gz")
    assert __import__("pathlib").Path(result["backup"]["database"]).exists()
    conn = connect(db, readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id='old-session'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id='new-session'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_disk_pressure_marks_retention_truncated(tmp_path):
    now_us = time.time_ns() // 1000
    _seed(tmp_path, now_us)
    Usage = namedtuple("Usage", "total used free")

    plan = plan_retention(
        tmp_path,
        now_us=now_us,
        disk_usage=lambda _path: Usage(10 * 1024**3, 9_500 * 1024**2, 500 * 1024**2),
    )

    assert plan["disk"]["pressure"] is True
    assert plan["retention_truncated"] is True


def test_retention_reclaims_the_only_segment_of_a_closed_producer(tmp_path):
    now_us = time.time_ns() // 1000
    spool = tmp_path / "state" / "metrics" / "spool" / "producer-closed"
    segment = spool / "one.telemetry"
    with SpoolWriter(segment) as writer:
        writer.append(_session_event(
            "producer-closed", 0, "old-session", now_us - 40 * DAY_US
        ))
    (spool / "health.json").write_text(json.dumps({
        "producer_id": "producer-closed",
        "pid": os.getpid(),
        "writer_closed": True,
    }))
    MetricsCollector(tmp_path / "state" / "metrics" / "metrics.db").collect_segment(
        segment
    )
    old_time = (now_us - 40 * DAY_US) / 1_000_000
    os.utime(segment, (old_time, old_time))

    plan = plan_retention(tmp_path, now_us=now_us)

    assert [item["path"] for item in plan["archive_segments"]] == [str(segment)]


def test_retention_reclaims_fully_imported_segment_after_sigkill(
    tmp_path, monkeypatch
):
    now_us = time.time_ns() // 1000
    spool = tmp_path / "state" / "metrics" / "spool" / "producer-dead"
    segment = spool / "one.telemetry"
    with SpoolWriter(segment) as writer:
        writer.append(_session_event(
            "producer-dead", 0, "old-session", now_us - 40 * DAY_US
        ))
    (spool / "health.json").write_text(json.dumps({
        "producer_id": "producer-dead",
        "pid": 424242,
        "writer_closed": False,
    }))
    monkeypatch.setattr("bobi.metrics.retention._process_alive", lambda _pid: False)
    MetricsCollector(tmp_path / "state" / "metrics" / "metrics.db").collect_segment(
        segment
    )
    old_time = (now_us - 40 * DAY_US) / 1_000_000
    os.utime(segment, (old_time, old_time))

    plan = plan_retention(tmp_path, now_us=now_us)

    assert [item["path"] for item in plan["archive_segments"]] == [str(segment)]


def test_retention_handles_session_dependencies_without_orphaning_children(tmp_path):
    now_us = time.time_ns() // 1000
    db, _old, _current = _seed(tmp_path, now_us)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO workflow_steps(workflow_step_id,session_id,workflow_name,"
            "step_name,attempt,step_type,started_at_us,status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("step-old", "old-session", "workflow", "step", 1, "native", 1, "completed"),
        )
        conn.execute(
            "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,"
            "provider,amount_usd,measurement_source,is_estimated,observed_at_us) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("cost-old", "session", "old-session", "stub", 1.0, "provider_stream", 0, 1),
        )
        conn.execute(
            "INSERT INTO sessions(session_id,session_name,brain,provider,"
            "started_at_us,status,parent_session_id) VALUES(?,?,?,?,?,?,?)",
            ("child-session", "child", "stub", "stub", now_us, "running", "old-session"),
        )
        conn.commit()
    finally:
        conn.close()

    result = apply_retention(tmp_path, now_us=now_us)

    assert result["status"] == "done"
    conn = connect(db, readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id='old-session'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_steps WHERE workflow_step_id='step-old'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM cost_measurements WHERE cost_measurement_id='cost-old'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
