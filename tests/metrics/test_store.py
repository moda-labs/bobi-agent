import pytest

from bobi.fsutil import try_file_lock
from bobi.metrics.collector import MetricsCollector, rebuild_database
from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import HEADER, SpoolCorruptionError, SpoolWriter, iter_frames
from bobi.metrics.store import (
    connect,
    integrity_check,
    logical_snapshot,
    migrate,
    stage_frames,
)


def _event(event_type, sequence, payload, **ids):
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
        _event(
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
        _event(
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
        _event(
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


def test_collector_replay_is_idempotent_and_rebuild_is_deterministic(tmp_path):
    segment = tmp_path / "segment.telemetry"
    db = tmp_path / "metrics.db"
    rebuilt = tmp_path / "rebuilt.db"
    _write_complete_segment(segment)
    collector = MetricsCollector(db)
    first = collector.collect_segment(segment)
    second = collector.collect_segment(segment)
    assert first["staged"] == 3
    assert first["projected"] == 3
    assert second["staged"] == 0
    conn = connect(db, readonly=True)
    before = logical_snapshot(conn)
    assert integrity_check(conn) == "ok"
    conn.close()
    after = rebuild_database(db, rebuilt)
    assert after == before


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
                _event(
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
            _event(
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
            _event(
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
    original = _event(
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
    original = _event(
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
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
    conn.close()
