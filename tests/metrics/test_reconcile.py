import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bobi.metrics.collector import MetricsCollector
from bobi.metrics.events import MetricsEvent
from bobi.metrics.providers import (
    ProviderContractError,
    parse_claude_transcript_records,
    parse_codex_rollout_records,
)
from bobi.metrics.reconcile import _aggregate, reconcile_missing, reconcile_turn
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect


FIXTURES = Path(__file__).parents[1] / "fixtures" / "metrics"


def _us(value: str) -> int:
    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
        * 1_000_000
    )


def _event(event_type, sequence, payload, **ids):
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
        _event(
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
        _event(
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
        events.append(_event(
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


def test_provider_record_parsers_preserve_correlation_and_deduplicate():
    claude = parse_claude_transcript_records(FIXTURES / "claude-transcript.jsonl")
    codex = parse_codex_rollout_records(FIXTURES / "codex-rollout.jsonl")

    assert [record.usage.provider_event_id for record in claude] == [
        "msg-fixture-1",
        "msg-fixture-sidechain",
    ]
    assert all(record.provider_session_id == "fixture-claude-session" for record in claude)
    assert len(codex) == 1
    assert codex[0].provider_session_id == "fixture-codex-thread"
    assert codex[0].provider_turn_id == "fixture-codex-turn"
    assert codex[0].usage.input_tokens == 4096


def test_reconciled_turn_aggregate_preserves_unreported_dimensions_as_null():
    record = parse_claude_transcript_records(
        FIXTURES / "claude-transcript.jsonl"
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


def test_codex_rollout_parser_rejects_unknown_versions(tmp_path):
    source = FIXTURES / "codex-rollout.jsonl"
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


def test_claude_reconciliation_is_idempotent_and_supersedes_estimate(
    monkeypatch, tmp_path
):
    root = tmp_path / "agent"
    turn_id = "turn-claude"
    estimate = {
        "measurement_id": "estimate-claude",
        "scope": "turn",
        "turn_id": turn_id,
        "invocation_id": None,
        "provider": "anthropic",
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
        provider="anthropic",
        brain="claude",
        provider_session_id="fixture-claude-session",
        turn_id=turn_id,
        estimate=estimate,
    )
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_claude_transcript",
        lambda _session_id: FIXTURES / "claude-transcript.jsonl",
    )

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
        assert exact["measurement_source"] == "claude_transcript"
        assert exact["supersedes_measurement_id"] == "estimate-claude"
        assert exact["cache_write_1h_input_tokens"] == 8192
    finally:
        conn.close()


def test_codex_reconciliation_uses_final_turn_usage(monkeypatch, tmp_path):
    root = tmp_path / "agent"
    turn_id = "turn-codex"
    estimate = {
        "measurement_id": "estimate-codex",
        "scope": "turn",
        "turn_id": turn_id,
        "invocation_id": None,
        "provider": "openai",
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
        provider="openai",
        brain="codex",
        provider_session_id="fixture-codex-thread",
        turn_id=turn_id,
        estimate=estimate,
    )
    monkeypatch.setattr(
        "bobi.metrics.reconcile.find_codex_rollout",
        lambda _session_id: FIXTURES / "codex-rollout.jsonl",
    )

    result = reconcile_turn(root, turn_id, wait=True)

    assert result["exact_measurements_recovered"] == 1
    conn = connect(root / "state" / "metrics" / "metrics.db", readonly=True)
    try:
        exact = conn.execute(
            "SELECT * FROM best_usage WHERE turn_id=? AND scope='turn'",
            (turn_id,),
        ).fetchone()
        assert exact["measurement_source"] == "codex_rollout"
        assert exact["input_tokens"] == 4096
        assert exact["cache_read_input_tokens"] == 1024
        assert exact["reasoning_output_tokens"] == 12
        assert exact["supersedes_measurement_id"] == "estimate-codex"
    finally:
        conn.close()


def test_reconciliation_recovers_provider_session_id_from_runtime_state(
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
        lambda _session_id: FIXTURES / "claude-transcript.jsonl",
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


def test_reconciliation_reserves_stable_sequences_with_partial_invocations(
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
        lambda _session_id: FIXTURES / "claude-transcript.jsonl",
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
