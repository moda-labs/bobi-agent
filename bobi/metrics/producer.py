"""Bounded non-blocking producer and out-of-band spool writer."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import SpoolWriter


class MetricsProducer:
    """Keep all disk and durability work off the agent turn path."""

    def __init__(self, segment_path: Path | str, *, capacity: int = 8192) -> None:
        self._queue: queue.Queue[MetricsEvent | None] = queue.Queue(maxsize=capacity)
        self._writer = SpoolWriter(segment_path)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()
        self._started = False
        self._closed = False
        self.telemetry_events_dropped = 0
        self.writer_errors = 0

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
                    continue
                try:
                    self._writer.append(event)
                except Exception:
                    self.writer_errors += 1
        finally:
            try:
                self._writer.close()
            except Exception:
                self.writer_errors += 1
