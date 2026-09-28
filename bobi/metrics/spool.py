"""Length/checksum-framed append-only telemetry segments."""

from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from bobi.metrics.events import MetricsEvent

MAGIC = b"BMT1"
TERMINATOR = b"\n"
HEADER = struct.Struct(">4sHI32s")
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class SpoolCorruptionError(ValueError):
    """A complete spool frame violates the framing contract."""


def _sync(fd: int) -> None:
    sync = getattr(os, "fdatasync", os.fsync)
    sync(fd)


@dataclass(frozen=True)
class FrameRecord:
    event: MetricsEvent
    offset: int
    next_offset: int
    payload_sha256: str


def encode_frame(event: MetricsEvent) -> bytes:
    payload = event.to_bytes()
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("metrics event exceeds maximum spool payload")
    digest = hashlib.sha256(payload).digest()
    return HEADER.pack(MAGIC, event.schema_version, len(payload), digest) + payload + TERMINATOR


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    return stream.read(size)


def iter_frames(path: Path | str, start_offset: int = 0) -> Iterator[FrameRecord]:
    """Yield valid frames and tolerate only one incomplete final frame."""
    with Path(path).open("rb") as stream:
        stream.seek(start_offset)
        while True:
            offset = stream.tell()
            header = _read_exact(stream, HEADER.size)
            if not header:
                return
            if len(header) < HEADER.size:
                return
            magic, schema_version, payload_length, expected_digest = HEADER.unpack(header)
            if magic != MAGIC:
                raise SpoolCorruptionError(f"invalid frame magic at offset {offset}")
            if payload_length > MAX_PAYLOAD_BYTES:
                raise SpoolCorruptionError(f"impossible frame length at offset {offset}")
            payload = _read_exact(stream, payload_length)
            terminator = _read_exact(stream, 1)
            if len(payload) < payload_length or not terminator:
                return
            if terminator != TERMINATOR:
                raise SpoolCorruptionError(f"invalid frame terminator at offset {offset}")
            digest = hashlib.sha256(payload).digest()
            if digest != expected_digest:
                raise SpoolCorruptionError(f"frame checksum mismatch at offset {offset}")
            try:
                event = MetricsEvent.from_bytes(payload)
            except ValueError as exc:
                raise SpoolCorruptionError(
                    f"invalid frame payload at offset {offset}"
                ) from exc
            if event.schema_version != schema_version:
                raise SpoolCorruptionError(f"frame schema mismatch at offset {offset}")
            yield FrameRecord(
                event=event,
                offset=offset,
                next_offset=stream.tell(),
                payload_sha256=digest.hex(),
            )


class SpoolWriter:
    """Own one append-only segment and never share it across processes."""

    def __init__(
        self,
        path: Path | str,
        *,
        sync_every_frame: bool = False,
        sync_interval_seconds: float = 0.25,
        sync_bytes: int = 64 * 1024,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self._sync_every_frame = sync_every_frame
        self._sync_interval_seconds = sync_interval_seconds
        self._sync_bytes = sync_bytes
        self._bytes_since_sync = 0
        self._last_sync = time.monotonic()
        self._failed = False

    def append(self, event: MetricsEvent) -> int:
        if self._failed:
            raise OSError("spool writer is unavailable after a prior append failure")
        frame = encode_frame(event)
        offset = os.lseek(self._fd, 0, os.SEEK_END)
        view = memoryview(frame)
        written = 0
        try:
            while written < len(frame):
                count = os.write(self._fd, view[written:])
                if count <= 0:
                    raise OSError(f"short spool append: {written}/{len(frame)} bytes")
                written += count
        except Exception:
            self._failed = True
            raise
        self._bytes_since_sync += len(frame)
        if (
            self._sync_every_frame
            or self._bytes_since_sync >= self._sync_bytes
            or time.monotonic() - self._last_sync >= self._sync_interval_seconds
        ):
            self.flush()
        return offset

    def flush(self) -> None:
        if self._failed:
            raise OSError("spool writer is unavailable after a prior append failure")
        try:
            _sync(self._fd)
        except Exception:
            self._failed = True
            raise
        self._bytes_since_sync = 0
        self._last_sync = time.monotonic()

    def flush_if_due(self) -> bool:
        if (
            self._bytes_since_sync
            and time.monotonic() - self._last_sync >= self._sync_interval_seconds
        ):
            self.flush()
            return True
        return False

    def close(self) -> None:
        if self._fd >= 0:
            fd = self._fd
            self._fd = -1
            try:
                _sync(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> "SpoolWriter":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
