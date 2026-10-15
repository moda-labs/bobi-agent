"""Bounded non-blocking producer and out-of-band spool writer."""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json
from bobi.metrics.events import MetricsEvent
from bobi.metrics.spool import SpoolWriter


def process_start_token(pid: int) -> int | None:
    """A value that names one process among all that have held *pid*, or None
    when it cannot be read (no such process, or an unsupported platform).

    Linux: the start in clock ticks after boot, field 22 of
    ``/proc/<pid>/stat``. macOS: ``kp_proc.p_starttime`` from
    ``sysctl(KERN_PROC_PID)``, in microseconds. Each is fixed when the process
    starts and is compared only with itself, never with a wall clock.
    """
    if sys.platform.startswith("linux"):
        return _proc_start_ticks(pid)
    if sys.platform == "darwin":
        return _darwin_start_us(pid)
    return None


def _proc_start_ticks(pid: int, proc: Path = Path("/proc")) -> int | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        # The command name is in parentheses and may itself hold spaces or
        # parentheses; the fields after the last one start at field 3.
        return int(stat[stat.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


_KINFO_PROC_SIZE = 648


def _darwin_start_us(pid: int) -> int | None:
    """The ``struct timeval`` at the start of ``struct kinfo_proc``."""
    try:
        sysctl = _libc().sysctl
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buffer = ctypes.create_string_buffer(_KINFO_PROC_SIZE)
        size = ctypes.c_size_t(_KINFO_PROC_SIZE)
        if sysctl(mib, 4, buffer, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    if size.value != _KINFO_PROC_SIZE:
        return None  # no such process, or a layout this code does not know
    seconds, microseconds = struct.unpack_from("=qi", buffer, 0)
    if seconds <= 0 or not 0 <= microseconds < 1_000_000:
        return None
    return seconds * 1_000_000 + microseconds


@functools.lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)


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
        self._writer_failed = threading.Event()
        self._started = False
        self._closed = False
        self._writer_closed = False
        self._started_at_us = time.time_ns() // 1000
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
        if not self._started or self._closed or self._writer_failed.is_set():
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
            "writer_available": not self._writer_failed.is_set(),
            "writer_closed": self._writer_closed,
            "pid": os.getpid(),
            "started_at_us": self._started_at_us,
            "heartbeat_at_us": time.time_ns() // 1000,
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
                        self._writer_failed.set()
                    self._write_health()
                    continue
                try:
                    self._writer.append(event)
                    self.events_written += 1
                    self.last_write_at_us = time.time_ns() // 1000
                except Exception:
                    self.writer_errors += 1
                    self._writer_failed.set()
        finally:
            try:
                self._writer.close()
            except Exception:
                self.writer_errors += 1
            self._writer_closed = True
            self._write_health(force=True)
