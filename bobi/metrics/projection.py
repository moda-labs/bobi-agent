"""Deferred idempotent projection from immutable raw metrics events."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from bobi.metrics.store import raw_payload

ORPHAN_TTL_US = 24 * 60 * 60 * 1_000_000


@dataclass(frozen=True)
class ProjectionTarget:
    table: str
    primary_key: str
    priority: int


TARGETS = {
    "session.recorded": ProjectionTarget("sessions", "session_id", 10),
    "turn.recorded": ProjectionTarget("turns", "turn_id", 20),
    "workflow_step.recorded": ProjectionTarget(
        "workflow_steps", "workflow_step_id", 20
    ),
    "router_decision.recorded": ProjectionTarget(
        "router_decisions", "router_decision_id", 20
    ),
    "invocation.recorded": ProjectionTarget(
        "llm_invocations", "invocation_id", 30
    ),
    "tool_execution.recorded": ProjectionTarget(
        "tool_executions", "tool_execution_id", 40
    ),
    "usage.recorded": ProjectionTarget("usage_measurements", "measurement_id", 40),
    "price_snapshot.recorded": ProjectionTarget(
        "price_snapshots", "price_snapshot_id", 40
    ),
    "cost.recorded": ProjectionTarget(
        "cost_measurements", "cost_measurement_id", 40
    ),
    "experiment_outcome.recorded": ProjectionTarget(
        "experiment_outcomes", "outcome_id", 40
    ),
}


class MissingParentError(ValueError):
    pass


PARENTS = {
    "session.recorded": (("sessions", "session_id", "parent_session_id", False),),
    "turn.recorded": (("sessions", "session_id", "session_id", True),),
    "workflow_step.recorded": (
        ("sessions", "session_id", "session_id", True),
        ("turns", "turn_id", "turn_id", False),
    ),
    "router_decision.recorded": (("turns", "turn_id", "turn_id", True),),
    "invocation.recorded": (
        ("turns", "turn_id", "turn_id", True),
        ("workflow_steps", "workflow_step_id", "workflow_step_id", False),
        ("router_decisions", "router_decision_id", "router_decision_id", False),
        ("llm_invocations", "invocation_id", "parent_invocation_id", False),
    ),
    "tool_execution.recorded": (
        ("turns", "turn_id", "turn_id", True),
        ("llm_invocations", "invocation_id", "triggering_invocation_id", True),
        ("llm_invocations", "invocation_id", "consuming_invocation_id", False),
    ),
    "usage.recorded": (
        ("turns", "turn_id", "turn_id", True),
        ("llm_invocations", "invocation_id", "invocation_id", False),
    ),
    "cost.recorded": (
        ("sessions", "session_id", "session_id", False),
        ("turns", "turn_id", "turn_id", False),
        ("llm_invocations", "invocation_id", "invocation_id", False),
        ("price_snapshots", "price_snapshot_id", "price_snapshot_id", False),
    ),
    "experiment_outcome.recorded": (
        ("router_decisions", "router_decision_id", "router_decision_id", True),
    ),
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _project_row(
    conn: sqlite3.Connection,
    target: ProjectionTarget,
    payload: dict[str, object],
) -> None:
    if target.primary_key not in payload:
        raise ValueError(f"missing {target.primary_key}")
    if target.table == "router_decisions" and payload.get("experiment_id"):
        existing = conn.execute(
            "SELECT router_name,router_version,policy_version,feature_schema_version,"
            "candidate_models_json,control_model,metadata_json FROM router_decisions "
            "WHERE experiment_id=? LIMIT 1",
            (payload["experiment_id"],),
        ).fetchone()
        immutable = (
            "router_name", "router_version", "policy_version",
            "feature_schema_version", "candidate_models_json", "control_model",
        )
        if existing is not None and any(
            existing[name] != payload.get(name) for name in immutable
        ):
            raise ValueError(
                "experiment configuration changed for an existing experiment_id"
            )
        if existing is not None:
            try:
                prior = json.loads(existing["metadata_json"])
                current = json.loads(str(payload.get("metadata_json") or "{}"))
            except (TypeError, ValueError):
                raise ValueError("router metadata_json must be valid JSON") from None
            if prior.get("config_fingerprint") != current.get("config_fingerprint"):
                raise ValueError("experiment config fingerprint changed")
    allowed = _columns(conn, target.table)
    values = {key: value for key, value in payload.items() if key in allowed}
    names = list(values)
    placeholders = ",".join("?" for _ in names)
    updates = []
    for name in names:
        if name == target.primary_key:
            continue
        if target.table == "sessions" and name == "provider_session_id":
            updates.append(
                "provider_session_id=COALESCE("
                "excluded.provider_session_id,sessions.provider_session_id)"
            )
        else:
            updates.append(f"{name}=excluded.{name}")
    conflict = (
        f"DO UPDATE SET {','.join(updates)}"
        if updates
        else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO {target.table}({','.join(names)}) "
        f"VALUES ({placeholders}) ON CONFLICT({target.primary_key}) {conflict}",
        [values[name] for name in names],
    )


def _require_parents(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, object],
) -> None:
    for table, primary_key, payload_key, required in PARENTS.get(event_type, ()):
        value = payload.get(payload_key)
        if value is None:
            if required:
                raise ValueError(f"missing required parent key {payload_key}")
            continue
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE {primary_key}=?", (value,)
        ).fetchone()
        if exists is None:
            raise MissingParentError(f"missing {table}.{primary_key}={value}")


def _requeue_resolved_orphans(
    conn: sqlite3.Connection,
    projected_parents: set[tuple[str, str, object]],
) -> None:
    """Wake only TTL-quarantined rows whose newly arrived parents now exist."""
    candidate_ids: set[str] = set()
    for event_type, parents in PARENTS.items():
        for table, primary_key, payload_key, _required in parents:
            values = {
                value
                for parent_table, parent_key, value in projected_parents
                if parent_table == table and parent_key == primary_key
            }
            for value in values:
                rows = conn.execute(
                    """SELECT event_id FROM raw_events
                       WHERE projection_state='quarantined'
                         AND projection_error='missing_parent_ttl_expired'
                         AND event_type=?
                         AND json_extract(payload_json, ?) = ?""",
                    (event_type, f"$.{payload_key}", value),
                ).fetchall()
                candidate_ids.update(str(row["event_id"]) for row in rows)

    for event_id in candidate_ids:
        row = conn.execute(
            "SELECT event_type,payload_json FROM raw_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            _require_parents(
                conn,
                str(row["event_type"]),
                json.loads(str(row["payload_json"])),
            )
        except MissingParentError:
            continue
        conn.execute(
            """UPDATE raw_events SET projection_state='pending',
               projection_not_before_us=NULL, projection_error='missing_parent'
               WHERE event_id=?""",
            (event_id,),
        )


def project_pending(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    now_us: int | None = None,
) -> dict[str, int]:
    """Project one bounded batch while isolating malformed and missing-parent rows."""
    now_us = now_us if now_us is not None else time.time_ns() // 1000
    rows = conn.execute(
        """SELECT * FROM raw_events
           WHERE projection_state='pending'
             AND (projection_not_before_us IS NULL OR projection_not_before_us <= ?)
           ORDER BY CASE event_type
               WHEN 'session.recorded' THEN 10
               WHEN 'turn.recorded' THEN 20
               WHEN 'workflow_step.recorded' THEN 20
               WHEN 'router_decision.recorded' THEN 20
               WHEN 'invocation.recorded' THEN 30
               ELSE 40 END,
               emitted_at_us, producer_id, producer_sequence,
               received_at_us, event_id
           LIMIT ?""",
        (now_us, limit),
    ).fetchall()
    result = {
        "selected": len(rows),
        "projected": 0,
        "deferred": 0,
        "quarantined": 0,
    }
    projected_parents: set[tuple[str, str, object]] = set()
    deadline_ns = time.perf_counter_ns() + 25_000_000
    conn.execute("BEGIN")
    try:
        for row in rows:
            if time.perf_counter_ns() >= deadline_ns and result["projected"]:
                break
            target = TARGETS.get(row["event_type"])
            if target is None:
                conn.execute(
                    """UPDATE raw_events SET projection_state='quarantined',
                       projection_attempts=projection_attempts+1,
                       projection_error='unsupported_event_type'
                       WHERE event_id=?""",
                    (row["event_id"],),
                )
                result["quarantined"] += 1
                continue
            conn.execute("SAVEPOINT project_one")
            try:
                payload = raw_payload(row)
                _require_parents(conn, row["event_type"], payload)
                _project_row(conn, target, payload)
            except MissingParentError:
                conn.execute("ROLLBACK TO project_one")
                conn.execute("RELEASE project_one")
                attempts = int(row["projection_attempts"]) + 1
                if now_us - int(row["received_at_us"]) >= ORPHAN_TTL_US:
                    conn.execute(
                        """UPDATE raw_events SET projection_state='quarantined',
                           projection_attempts=?, projection_not_before_us=NULL,
                           projection_error='missing_parent_ttl_expired'
                           WHERE event_id=?""",
                        (attempts, row["event_id"]),
                    )
                    result["quarantined"] += 1
                    continue
                backoff_us = min(60, 2 ** min(attempts, 6)) * 1_000_000
                conn.execute(
                    """UPDATE raw_events SET projection_attempts=?,
                       projection_not_before_us=?, projection_error='missing_parent'
                       WHERE event_id=?""",
                    (attempts, now_us + backoff_us, row["event_id"]),
                )
                result["deferred"] += 1
                continue
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK TO project_one")
                conn.execute("RELEASE project_one")
                conn.execute(
                    """UPDATE raw_events SET projection_state='quarantined',
                       projection_attempts=projection_attempts+1,
                       projection_error=? WHERE event_id=?""",
                    (f"integrity_error:{exc}", row["event_id"]),
                )
                result["quarantined"] += 1
                continue
            except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
                conn.execute("ROLLBACK TO project_one")
                conn.execute("RELEASE project_one")
                conn.execute(
                    """UPDATE raw_events SET projection_state='quarantined',
                       projection_attempts=projection_attempts+1,
                       projection_error=? WHERE event_id=?""",
                    (f"projection_error:{exc}", row["event_id"]),
                )
                result["quarantined"] += 1
                continue
            conn.execute("RELEASE project_one")
            conn.execute(
                """UPDATE raw_events SET projection_state='projected',
                   projected_at_us=?, projection_error=NULL,
                   projection_not_before_us=NULL WHERE event_id=?""",
                (now_us, row["event_id"]),
            )
            result["projected"] += 1
            projected_parents.add(
                (target.table, target.primary_key, payload[target.primary_key])
            )
        if result["projected"]:
            conn.execute(
                """UPDATE raw_events SET projection_not_before_us=NULL
                   WHERE projection_state='pending'
                     AND projection_error='missing_parent'"""
            )
            _requeue_resolved_orphans(conn, projected_parents)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result
