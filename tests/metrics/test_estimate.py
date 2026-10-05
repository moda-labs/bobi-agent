import json

import pytest

from bobi.metrics.collector import MetricsCollector
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
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect, migrate


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
