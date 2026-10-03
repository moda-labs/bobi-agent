"""Single-writer SQLite staging for durable metrics projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from bobi.metrics.events import MetricsEvent, canonical_json
from bobi.metrics.schema import SCHEMA_SQL, SCHEMA_VERSION
from bobi.metrics.spool import FrameRecord

def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if not readonly:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=5.0)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    current = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if current is not None and int(current[0]) > SCHEMA_VERSION:
        raise RuntimeError(f"metrics schema {current[0]} is newer than this Bobi")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _verify_duplicate(
    conn: sqlite3.Connection,
    event: MetricsEvent,
    segment_path: str,
    segment_offset: int,
) -> None:
    rows = conn.execute(
        "SELECT event_id, schema_version, event_type, session_id, turn_id, "
        "invocation_id, source, payload_sha256, producer_id, producer_sequence, "
        "segment_path, segment_offset FROM raw_events WHERE event_id=? "
        "OR (producer_id=? AND producer_sequence=?) "
        "OR (segment_path=? AND segment_offset=?)",
        (
            event.event_id,
            event.producer_id,
            event.producer_sequence,
            segment_path,
            segment_offset,
        ),
    ).fetchall()
    payload_sha256 = hashlib.sha256(canonical_json(event.payload)).hexdigest()
    identity = {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "invocation_id": event.invocation_id,
        "source": event.source,
        "payload_sha256": payload_sha256,
    }
    if not rows or any(
        any(row[key] != value for key, value in identity.items()) for row in rows
    ):
        raise ValueError("metrics event conflicts with an existing deduplication key")


def stage_frames(
    conn: sqlite3.Connection,
    *,
    segment_path: Path | str,
    frames: Iterable[FrameRecord],
) -> int:
    """Durably stage complete frames and advance the cursor in one transaction."""
    path = str(Path(segment_path))
    received_at_us = time.time_ns() // 1000
    staged = 0
    last_frame: FrameRecord | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        for frame in frames:
            event = frame.event
            payload_json = canonical_json(event.payload).decode("utf-8")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO raw_events(
                    event_id, schema_version, event_type, producer_id,
                    producer_sequence, emitted_at_us, received_at_us,
                    session_id, turn_id, invocation_id, source, payload_json,
                    payload_sha256, segment_path, segment_offset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type,
                    event.producer_id,
                    event.producer_sequence,
                    event.emitted_at_us,
                    received_at_us,
                    event.session_id,
                    event.turn_id,
                    event.invocation_id,
                    event.source,
                    payload_json,
                    hashlib.sha256(payload_json.encode()).hexdigest(),
                    path,
                    frame.offset,
                ),
            )
            if cursor.rowcount:
                staged += 1
            else:
                _verify_duplicate(conn, event, path, frame.offset)
            last_frame = frame
        if last_frame is not None:
            conn.execute(
                """INSERT INTO spool_cursors(
                    segment_path, producer_id, committed_offset,
                    last_event_id, updated_at_us
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(segment_path) DO UPDATE SET
                    producer_id=excluded.producer_id,
                    committed_offset=excluded.committed_offset,
                    last_event_id=excluded.last_event_id,
                    updated_at_us=excluded.updated_at_us""",
                (
                    path,
                    last_frame.event.producer_id,
                    last_frame.next_offset,
                    last_frame.event.event_id,
                    received_at_us,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return staged


def cursor_offset(conn: sqlite3.Connection, segment_path: Path | str) -> int:
    row = conn.execute(
        "SELECT committed_offset FROM spool_cursors WHERE segment_path=?",
        (str(Path(segment_path)),),
    ).fetchone()
    return int(row[0]) if row else 0


def integrity_check(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def raw_payload(row: sqlite3.Row) -> dict[str, object]:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("raw event payload must be an object")
    return payload
