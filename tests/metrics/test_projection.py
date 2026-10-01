import json
from pathlib import Path

from bobi.metrics.collector import MetricsCollector, rebuild_database
from bobi.metrics.events import MetricsEvent
from bobi.metrics.projection import ORPHAN_TTL_US, project_pending
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

def test_historical_session_assignment_survives_projection_and_rebuild(tmp_path):
    assignment = json.loads(Path(
        "tests/fixtures/metrics/historical-router-assignment.json"
    ).read_text())
    segment = tmp_path / "historical.telemetry"
    database = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(_event("session.recorded", 1, {
            "session_id": "historical-session", "session_name": "agent",
            "brain": "codex", "provider": "openai", "started_at_us": 1,
            "status": "running",
            "metadata_json": json.dumps({"router_assignment": assignment}),
        }, session_id="historical-session"))
        writer.append(_event("turn.recorded", 2, {
            "turn_id": "historical-turn", "session_id": "historical-session",
            "turn_index": 1, "trigger_kind": "user", "is_user_initiated": 1,
            "started_at_us": 2, "status": "completed",
        }, session_id="historical-session", turn_id="historical-turn"))
    MetricsCollector(database).collect_segment(segment)
    rebuilt = tmp_path / "rebuilt.db"
    rebuild_database(database, rebuilt)
    for path in (database, rebuilt):
        conn = connect(path, readonly=True)
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


def test_missing_parent_is_quarantined_after_orphan_ttl(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    with SpoolWriter(segment) as writer:
        writer.append(_event(
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
        writer.append(_event(
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
        writer.append(_event(
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
        writer.append(_event(
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
        writer.append(_event(
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
        writer.append(_event("session.recorded", 1, session, session_id="s1"))
        writer.append(_event("turn.recorded", 2, turns[0], session_id="s1", turn_id="t1"))
        writer.append(_event("router_decision.recorded", 3, decision, session_id="s1", turn_id="t1"))
        writer.append(_event("turn.recorded", 4, turns[1], session_id="s1", turn_id="t2"))
        writer.append(_event("router_decision.recorded", 5, changed, session_id="s1", turn_id="t2"))

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
