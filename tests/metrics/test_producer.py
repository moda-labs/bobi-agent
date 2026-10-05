import threading
import time

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


def test_producer_contains_spool_failures_after_startup(tmp_path):
    class BrokenWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            raise OSError("disk unavailable")

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry", writer_factory=BrokenWriter
    )
    producer.start()

    assert producer.try_emit(_event()) is True
    deadline = time.monotonic() + 1
    while producer.writer_errors == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert producer.try_emit(_event(2)) is False
    assert producer.close(timeout=2) is True
    assert producer.writer_errors == 1
    assert producer.health()["writer_available"] is False


def test_producer_shutdown_is_bounded_when_writer_is_stuck(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            entered.set()
            release.wait(2)

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry", writer_factory=BlockingWriter
    )
    producer.start()
    assert producer.try_emit(_event()) is True
    assert entered.wait(1) is True

    started = time.monotonic()
    assert producer.close(timeout=0.01) is False
    assert time.monotonic() - started < 0.25

    release.set()
    assert producer.close(timeout=1) is True


def test_queue_overflow_drops_without_blocking(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            entered.set()
            release.wait(2)

        def flush_if_due(self):
            return False

        def close(self):
            pass

    producer = MetricsProducer(
        tmp_path / "segment.telemetry",
        capacity=1,
        writer_factory=BlockingWriter,
    )
    producer.start()
    assert producer.try_emit(_event(1)) is True
    assert entered.wait(1) is True
    assert producer.try_emit(_event(2)) is True

    started = time.perf_counter_ns()
    assert producer.try_emit(_event(3)) is False
    assert (time.perf_counter_ns() - started) / 1000 < 200_000
    assert producer.telemetry_events_dropped == 1

    release.set()
    assert producer.close(timeout=1) is True
