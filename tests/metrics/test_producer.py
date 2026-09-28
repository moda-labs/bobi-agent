import threading

from bobi.metrics.events import MetricsEvent
from bobi.metrics.producer import MetricsProducer
from bobi.metrics.spool import iter_frames


def _event(sequence=1):
    return MetricsEvent(
        event_type="session.recorded",
        producer_id="producer",
        producer_sequence=sequence,
        source="test",
        payload={"session_id": "session"},
    )


def test_producer_writes_on_background_thread(tmp_path):
    segment = tmp_path / "segment.telemetry"
    producer = MetricsProducer(segment)
    producer.start()
    assert producer.try_emit(_event()) is True
    assert producer.close(timeout=2) is True
    assert [record.event.producer_sequence for record in iter_frames(segment)] == [1]


def test_producer_rejects_events_before_start_and_after_close(tmp_path):
    producer = MetricsProducer(tmp_path / "segment.telemetry")
    assert producer.try_emit(_event(1)) is False
    assert producer.close() is True
    assert producer.try_emit(_event(2)) is False
    assert producer.telemetry_events_dropped == 2


def test_producer_flushes_low_volume_queue_while_idle(monkeypatch, tmp_path):
    flushed = threading.Event()

    def flush_if_due(_writer):
        flushed.set()
        return True

    monkeypatch.setattr("bobi.metrics.spool.SpoolWriter.flush_if_due", flush_if_due)
    producer = MetricsProducer(tmp_path / "segment.telemetry")
    producer.start()
    assert producer.try_emit(_event()) is True
    assert flushed.wait(1) is True
    assert producer.close(timeout=2) is True
