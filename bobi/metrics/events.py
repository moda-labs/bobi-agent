"""Versioned metadata-only events for the metrics ingestion pipeline."""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import InitVar, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


def uuid7() -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7 on Python versions without ``uuid.uuid7``."""
    timestamp_ms = time.time_ns() // 1_000_000
    # UUID uniqueness does not require cryptographic randomness. Python seeds
    # the process-global PRNG from OS entropy and reseeds it after fork; using
    # it here avoids an entropy syscall for every telemetry envelope.
    random_bits = random.getrandbits(74)
    random_a = random_bits >> 62
    random_b = random_bits & ((1 << 62) - 1)
    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | random_a << 64
        | 0b10 << 62
        | random_b
    )
    return uuid.UUID(int=value)


def canonical_json(value: Any) -> bytes:
    """Return the stable UTF-8 representation used for IDs and spool checksums."""
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def deterministic_event_id(provider: str, provider_event_id: str) -> str:
    """Return a replay-stable ID for one provider-owned event."""
    material = canonical_json([provider, provider_event_id])
    return f"evt_{hashlib.sha256(material).hexdigest()}"


@dataclass(frozen=True)
class MetricsEvent:
    event_type: str
    producer_id: str
    producer_sequence: int
    source: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: f"evt_{uuid7()}")
    schema_version: int = SCHEMA_VERSION
    emitted_at_us: int = field(default_factory=lambda: time.time_ns() // 1000)
    session_id: str | None = None
    turn_id: str | None = None
    invocation_id: str | None = None
    _validate_payload: InitVar[bool] = True

    def __post_init__(self, _validate_payload: bool) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported metrics schema version: {self.schema_version}")
        if not self.event_type or not self.producer_id or not self.source:
            raise ValueError("event_type, producer_id, and source are required")
        if self.producer_sequence < 0:
            raise ValueError("producer_sequence must be non-negative")
        if not isinstance(self.payload, dict):
            raise ValueError("metrics event payload must be an object")
        if _validate_payload:
            canonical_json(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "producer_id": self.producer_id,
            "producer_sequence": self.producer_sequence,
            "emitted_at_us": self.emitted_at_us,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "source": self.source,
            "payload": self.payload,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "MetricsEvent":
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("metrics event payload is not valid UTF-8 JSON") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            raise ValueError("metrics event must be an object with an object payload")
        return cls(**raw)
