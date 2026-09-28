from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect


def _event(event_type, sequence, payload, **ids):
    return MetricsEvent(
        event_type=event_type,
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload=payload,
        **ids,
    )


def test_child_stages_before_parent_and_projects_after_parent_arrives(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    child = _event(
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
            _event(
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
            _event(
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
        _event("session.recorded", 1, session, session_id="s1"),
        _event("turn.recorded", 2, turn, session_id="s1", turn_id="t1"),
        _event("usage.recorded", 3, estimate, session_id="s1", turn_id="t1"),
        _event("usage.recorded", 4, exact, session_id="s1", turn_id="t1"),
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
