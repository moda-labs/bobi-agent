"""Single-writer SQLite staging for durable metrics projection."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable, NamedTuple

from bobi.metrics.events import MetricsEvent, canonical_json
from bobi.costs import model_identity
from bobi.metrics.schema import (
    BEST_USAGE_VERSION,
    MODEL_KEY_SOURCES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from bobi.metrics.spool import FrameRecord

log = logging.getLogger(__name__)

def _keep_private(db_path: Path) -> None:
    """Create the read model at 0600 and tighten one created looser.

    It copies every spool payload, so it is as private as the 0600 segments.
    SQLite creates ``-wal`` and ``-shm`` with the database file's mode; the
    existing ones are tightened too. This mode is the point, so the file is
    created here rather than through ``fsutil``'s atomic writes."""
    os.close(os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600))
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            if candidate.stat().st_mode & 0o077:
                os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue
        except PermissionError:
            # Another user's file: opening it still works, tightening is theirs to do.
            log.warning("metrics: %s is readable by other users and not ours to chmod", candidate.name)


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if not readonly:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _keep_private(db_path)
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


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:  # a fresh database has no schema_meta yet
        return None
    return None if row is None else row[0]


def _add_model_key_columns(conn: sqlite3.Connection) -> None:
    """Add ``model_key`` to a database created before the column existed."""
    for table in MODEL_KEY_SOURCES:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "model_key" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN model_key TEXT")


def fill_model_keys(conn: sqlite3.Connection) -> None:
    """Key every row written without one: rows from before the column existed,
    or rows written by hand or by an older Bobi rather than through projection.
    Runs on every migrate; a partial index keeps it to the unkeyed rows."""
    for table, column in MODEL_KEY_SOURCES.items():
        models = [row[0] for row in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE model_key IS NULL AND {column} IS NOT NULL")]
        conn.executemany(
            f"UPDATE {table} SET model_key=? WHERE model_key IS NULL AND {column}=?",
            [(model_identity(model), model) for model in models],
        )


def migrate(conn: sqlite3.Connection) -> None:
    # Refuse a newer database before touching it: an older Bobi must not
    # rewrite the view or any schema a newer one owns.
    current = _meta(conn, "schema_version")
    if current is not None and int(current) > SCHEMA_VERSION:
        raise RuntimeError(f"metrics schema {current} is newer than this Bobi")
    best_usage = _meta(conn, "best_usage_version")
    if best_usage is None or int(best_usage) != BEST_USAGE_VERSION:
        # The view holds no data; rebuild it from the current definition.
        conn.execute("DROP VIEW IF EXISTS best_usage")
    _add_model_key_columns(conn)
    conn.executescript(SCHEMA_SQL)
    fill_model_keys(conn)
    conn.executemany(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [("best_usage_version", str(BEST_USAGE_VERSION)), ("schema_version", str(SCHEMA_VERSION))],
    )
    conn.commit()


def _is_identical_duplicate(
    conn: sqlite3.Connection,
    event: MetricsEvent,
    segment_path: str,
    segment_offset: int,
) -> bool:
    """True when the rows that share this event's deduplication keys are this
    exact event; False when one of them says something else."""
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
    return bool(rows) and not any(
        any(row[key] != value for key, value in identity.items()) for row in rows
    )


class StageResult(NamedTuple):
    staged: int
    conflicts: int


def stage_frames(
    conn: sqlite3.Connection,
    *,
    segment_path: Path | str,
    frames: Iterable[FrameRecord],
) -> StageResult:
    """Durably stage complete frames and advance the cursor in one transaction.

    A frame whose deduplication keys already name a different event (a rerun
    reconcile after its session row changed) is set aside: the first event
    stands, the frame is logged and counted, and the cursor moves past it, so
    one conflict never stops the frames and segments after it.
    """
    path = str(Path(segment_path))
    received_at_us = time.time_ns() // 1000
    staged = 0
    conflicts = 0
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
            elif not _is_identical_duplicate(conn, event, path, frame.offset):
                conflicts += 1
                log.warning(
                    "metrics: set aside event %s at %s:%d; it conflicts with "
                    "an event already imported", event.event_id, path, frame.offset,
                )
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
    return StageResult(staged, conflicts)


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
