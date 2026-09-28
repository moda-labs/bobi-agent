"""Bounded non-blocking producer and out-of-band spool writer."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json
from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import SpoolWriter


class MetricsProducer:
    """Keep all disk and durability work off the agent turn path."""

    def __init__(
        self,
        segment_path: Path | str,
        *,
        capacity: int = 8192,
        health_path: Path | str | None = None,
        producer_id: str = "",
        writer_factory=SpoolWriter,
    ) -> None:
        self._queue: queue.Queue[MetricsEvent | None] = queue.Queue(maxsize=capacity)
        self._writer = writer_factory(segment_path)
        self._health_path = Path(health_path) if health_path is not None else None
        self._producer_id = producer_id
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()
        self._started = False
        self._closed = False
        self.telemetry_events_dropped = 0
        self.writer_errors = 0
        self.events_written = 0
        self.last_write_at_us: int | None = None
        self._last_health_write = 0.0

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("metrics producer is closed")
        if not self._started:
            self._thread.start()
            self._started = True

    def try_emit(self, event: MetricsEvent) -> bool:
        if not self._started or self._closed:
            self.telemetry_events_dropped += 1
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self.telemetry_events_dropped += 1
            return False

    def close(self, *, timeout: float = 2.0) -> bool:
        self._closed = True
        if not self._started:
            self._writer.close()
            return True
        self._stop.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def health(self) -> dict[str, int | bool | None]:
        return {
            "queue_depth": self._queue.qsize(),
            "telemetry_events_dropped": self.telemetry_events_dropped,
            "writer_errors": self.writer_errors,
            "events_written": self.events_written,
            "last_write_at_us": self.last_write_at_us,
            "writer_alive": self._thread.is_alive(),
        }

    def _write_health(self, *, force: bool = False) -> None:
        if self._health_path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_health_write < 1.0:
            return
        try:
            atomic_write_json(
                self._health_path,
                {"producer_id": self._producer_id, **self.health()},
                indent=None,
                sort_keys=True,
            )
            self._last_health_write = now
        except Exception:
            self.writer_errors += 1

    def _run(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.05)
                except queue.Empty:
                    try:
                        self._writer.flush_if_due()
                    except Exception:
                        self.writer_errors += 1
                    self._write_health()
                    continue
                try:
                    self._writer.append(event)
                    self.events_written += 1
                    self.last_write_at_us = time.time_ns() // 1000
                except Exception:
                    self.writer_errors += 1
        finally:
            try:
                self._writer.close()
            except Exception:
                self.writer_errors += 1
            self._write_health(force=True)
