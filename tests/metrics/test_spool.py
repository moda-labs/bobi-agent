import pytest
import uuid

from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import (
    HEADER,
    SpoolCorruptionError,
    SpoolWriter,
    encode_frame,
    iter_frames,
)


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
