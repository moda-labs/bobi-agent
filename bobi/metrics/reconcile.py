"""Out-of-band exact usage recovery from provider transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from bobi.chat_history import find_claude_transcript, find_codex_rollout
from bobi.metrics.collector import MetricsCollector, producer_exited
from bobi.metrics.events import MetricsEvent, deterministic_event_id, uuid7
from bobi.metrics.providers import (
    ProviderContractError,
    ProviderUsage,
    ProviderUsageRecord,
    parse_claude_transcript_records,
    parse_codex_rollout_records,
)
from bobi.metrics.spool import SpoolWriter
from bobi.metrics.store import connect
from bobi.sdk import load_session_id


class ReconciliationError(RuntimeError):
    """Exact provider facts cannot be correlated to the requested turn."""


@dataclass(frozen=True)
class ReconcilePlan:
    turn_id: str
    provider: str
    session_id: str
    source_path: Path
    events: tuple[MetricsEvent, ...]
    measurement_ids: tuple[str, ...]


def _stable_id(prefix: str, *parts: object) -> str:
    material = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()}"


def _sum_reported(values: Iterable[int | None]) -> int | None:
    """Sum a dimension only when every contributing invocation reports it."""
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _aggregate(records: list[ProviderUsageRecord]) -> list[ProviderUsageRecord]:
    grouped: dict[str, list[ProviderUsageRecord]] = defaultdict(list)
    for record in records:
        grouped[record.usage.model].append(record)
    result = []
    for model, group in sorted(grouped.items()):
        usages = [record.usage for record in group]
        complete = bool(usages) and all(
            usage.cache_write_breakdown_complete for usage in usages
        )
        cache_write = _sum_reported(
            usage.cache_write_input_tokens for usage in usages
        )
        incomplete = [
            usage for usage in usages if not usage.cache_write_breakdown_complete
        ]
        usage = ProviderUsage(
            provider=usages[0].provider,
            model=model,
            provider_event_id=f"aggregate:{model}",
            measurement_source=usages[0].measurement_source,
            scope="turn",
            input_tokens=_sum_reported(usage.input_tokens for usage in usages),
            uncached_input_tokens=_sum_reported(
                usage.uncached_input_tokens for usage in usages
            ),
            cache_read_input_tokens=_sum_reported(
                usage.cache_read_input_tokens for usage in usages
            ),
            cache_write_input_tokens=cache_write,
            cache_write_5m_input_tokens=_sum_reported(
                usage.cache_write_5m_input_tokens for usage in usages
            ),
            cache_write_1h_input_tokens=_sum_reported(
                usage.cache_write_1h_input_tokens for usage in usages
            ),
            cache_write_unknown_ttl_input_tokens=(
                None
                if complete
                else _sum_reported(
                    usage.cache_write_unknown_ttl_input_tokens
                    for usage in incomplete
                )
            ),
            cache_write_breakdown_complete=complete,
            output_tokens=_sum_reported(usage.output_tokens for usage in usages),
            reasoning_output_tokens=_sum_reported(
                usage.reasoning_output_tokens for usage in usages
            ),
            raw_usage={"events": [usage.raw_usage for usage in usages]},
            token_semantics_version=max(
                usage.token_semantics_version for usage in usages
            ),
        )
        result.append(ProviderUsageRecord(
            usage=usage,
            observed_at_us=max(record.observed_at_us for record in group),
            provider_session_id=group[-1].provider_session_id,
            provider_turn_id=group[-1].provider_turn_id,
        ))
    return result


class TurnStillRunning(ReconciliationError):
    """The turn's transcript may still grow: a recovery now would be partial."""


# The runtime producer that recorded a turn; served by the partial index
# idx_raw_turn_writer, not a raw_events scan.
_TURN_WRITER = (
    "SELECT producer_id FROM raw_events WHERE event_type='turn.recorded' "
    "AND turn_id={turn} AND producer_id LIKE 'producer-%' LIMIT 1"
)
_TURN_WRITER_SQL = _TURN_WRITER.format(turn="?")


def _writer_gone(producer_id: str | None) -> bool:
    """True once the process writing as *producer_id* exited. A turn whose
    writer cannot be found is not taken as abandoned."""
    return producer_id is not None and producer_exited(producer_id)


def _writer_exited(conn, turn_id: str) -> bool:
    """True once the runtime process that recorded the turn has exited."""
    row = conn.execute(_TURN_WRITER_SQL, (turn_id,)).fetchone()
    return _writer_gone(str(row[0]) if row is not None else None)


# The provider can stamp a turn's last transcript line a little after the
# stream that ended the turn. Nothing else widens a window past its end: a
# later turn's start is a hard bound, since its first call can land within
# seconds.
_END_SLACK_US = 5_000_000
# A turn's start is recorded before its request is sent, so no provider line
# of the turn is stamped earlier; this margin only absorbs clock rounding, and
# never reaches into the previous turn's window.
_START_SLACK_US = 2_000_000

_TURN_COLUMNS = "t.*,s.provider,s.brain,s.provider_session_id,s.session_name"


def _adjacent_turn(conn, row, provider_session_id: str, *, later: bool):
    """The nearest turn after (or before) *row* in its metrics session or its
    provider session, which a resumed process continues under a new metrics
    session; returns the same-session and other-session neighbours."""
    op, order = (">", "ASC") if later else ("<", "DESC")
    same_session = conn.execute(
        f"SELECT {_TURN_COLUMNS} FROM turns AS t JOIN sessions AS s USING(session_id) "
        f"WHERE t.session_id=? AND t.started_at_us{op}? "
        f"ORDER BY t.started_at_us {order} LIMIT 1",
        (row["session_id"], row["started_at_us"]),
    ).fetchone()
    other_session = conn.execute(
        f"SELECT {_TURN_COLUMNS} FROM sessions AS s JOIN turns AS t USING(session_id) "
        f"WHERE s.provider=? AND s.provider_session_id=? AND s.session_id<>? "
        f"AND t.started_at_us{op}? ORDER BY t.started_at_us {order} LIMIT 1",
        (row["provider"], provider_session_id, row["session_id"], row["started_at_us"]),
    ).fetchone() if provider_session_id else None
    return same_session, other_session


def _window_end(conn, row, provider_session_id: str) -> int:
    """The latest time a provider record can belong to the turn.

    A turn ends at the next turn of its metrics session or of its provider
    session. A turn without an end is over once its own process moved on, or
    once the process that recorded it exited; a later turn of another process
    does not end it while its writer still runs.
    """
    same_session, other_session = _adjacent_turn(conn, row, provider_session_id, later=True)
    next_starts = [int(turn["started_at_us"]) for turn in (same_session, other_session) if turn]
    if row["ended_at_us"] is not None:
        return min([int(row["ended_at_us"]) + _END_SLACK_US, *next_starts])
    if same_session is None and not _writer_exited(conn, str(row["turn_id"])):
        raise TurnStillRunning(
            f"turn {row['turn_id']} is still running; reconcile it after it ends"
        )
    # A turn whose process died never records its end; its transcript holds
    # nothing later than now.
    return min([time.time_ns() // 1000, *next_starts])


def _window_start(conn, row, provider_session_id: str) -> int:
    """The earliest time a provider record can belong to the turn.

    Adjacent windows partition: the turn starts strictly after the previous
    turn's window ends, whether or not that turn was reconciled yet."""
    started = int(row["started_at_us"])
    lower = started - _START_SLACK_US
    previous = max(
        (turn for turn in _adjacent_turn(conn, row, provider_session_id, later=False) if turn),
        key=lambda turn: int(turn["started_at_us"]),
        default=None,
    )
    if previous is None:
        return lower
    try:
        previous_end = _window_end(conn, previous, provider_session_id)
    except TurnStillRunning:
        # Another live process on the provider session: unsupported, so take
        # nothing from before this turn's own start.
        previous_end = started - 1
    return max(lower, previous_end + 1)


def _turn_context(conn, turn_id: str, root: Path | None):
    """The turn, its provider session id and its record window, inclusive."""
    row = conn.execute(
        f"SELECT {_TURN_COLUMNS} FROM turns AS t JOIN sessions AS s USING(session_id) "
        "WHERE t.turn_id=?",
        (turn_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationError(f"turn not found: {turn_id}")
    provider_session_id = str(row["provider_session_id"] or "")
    if not provider_session_id and root is not None:
        provider_session_id = load_session_id(str(row["session_name"]), root=root)
    upper = _window_end(conn, row, provider_session_id)
    return row, provider_session_id, _window_start(conn, row, provider_session_id), upper


def _select_records(
    conn,
    row,
    lower_us: int,
    upper_us: int,
    provider_session_id: str,
) -> tuple[Path, list[ProviderUsageRecord]]:
    provider = str(row["provider"])
    brain = str(row["brain"])
    if provider not in {"anthropic", "openai", "gateway"}:
        raise ReconciliationError(f"unsupported provider: {provider}")
    claude_source = find_claude_transcript(provider_session_id) if (
        provider == "anthropic" or provider == "gateway" and brain != "codex"
    ) else None
    codex_source = find_codex_rollout(provider_session_id) if (
        provider == "openai" or provider == "gateway" and brain != "claude"
    ) else None
    if provider == "gateway" and brain not in {"claude", "codex"}:
        if bool(claude_source) == bool(codex_source):
            raise ReconciliationError("gateway transcript format is missing or ambiguous")
    claude_format = provider == "anthropic" or provider == "gateway" and (
        brain == "claude" or brain not in {"claude", "codex"} and bool(claude_source)
    )
    if claude_format:
        source = claude_source
        if source is None:
            raise ReconciliationError(
                f"Claude transcript not found for {provider_session_id}"
            )
        records = _parsed(source, parse_claude_transcript_records)
    else:
        source = codex_source
        if source is None:
            raise ReconciliationError(
                f"Codex rollout not found for {provider_session_id}"
            )
        records = _parsed(source, parse_codex_rollout_records)
    records = [replace(record, usage=replace(record.usage, provider=provider)) for record in records]

    provider_turn_id = str(row["provider_turn_id"] or "")
    # A record the turn's own stream recorded is its, wherever the provider
    # stamped it: a last line can land after the next turn started.
    own = {
        str(item[0]) for item in conn.execute(
            "SELECT provider_event_id FROM llm_invocations WHERE turn_id=? "
            "AND provider_event_id IS NOT NULL",
            (row["turn_id"],),
        )
    }
    in_window = [
        record for record in records
        if lower_us <= record.observed_at_us <= upper_us
        or record.usage.provider_event_id in own
    ]
    used_elsewhere = _used_elsewhere(
        conn, provider, str(row["turn_id"]),
        {record.usage.provider_event_id for record in in_window},
    )
    selected = [
        record
        for record in in_window
        if record.usage.provider_event_id not in used_elsewhere
        and (
            not provider_turn_id
            or claude_format
            or record.provider_turn_id == provider_turn_id
        )
    ]
    if not claude_format and len(selected) > 1:
        selected = [max(selected, key=lambda item: item.observed_at_us)]
    if not selected:
        raise ReconciliationError(
            f"no exact provider usage matched turn {row['turn_id']}"
        )
    return source, selected


# One parse per transcript file and version: the turns of one session share it.
_PARSE_CACHE: dict[tuple[str, int, int], list[ProviderUsageRecord]] = {}
_PARSE_CACHE_SIZE = 8


def _parsed(path: Path, parse) -> list[ProviderUsageRecord]:
    stat = Path(path).stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _PARSE_CACHE:
        while len(_PARSE_CACHE) >= _PARSE_CACHE_SIZE:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
        _PARSE_CACHE[key] = parse(path)
    return _PARSE_CACHE[key]


def _used_elsewhere(conn, provider: str, turn_id: str, event_ids: set[str]) -> set[str]:
    """The candidate provider events another turn already holds, as a
    measurement or as an invocation its stream recorded."""
    ids = sorted(event_id for event_id in event_ids if event_id)
    used: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for table in ("usage_measurements", "llm_invocations"):
            used.update(str(item[0]) for item in conn.execute(
                f"SELECT provider_event_id FROM {table} WHERE provider=? AND turn_id<>? "
                f"AND provider_event_id IN ({marks})",
                (provider, turn_id, *chunk),
            ))
    return used


def _superseded_estimate(
    conn,
    *,
    turn_id: str,
    invocation_id: str | None,
    provider: str,
    model: str,
) -> str | None:
    row = conn.execute(
        "SELECT measurement_id FROM usage_measurements WHERE turn_id=? "
        "AND COALESCE(invocation_id,'')=COALESCE(?,'') "
        "AND provider=? AND model=? "
        "AND (is_estimated=1 OR measurement_source='provider_stream') "
        "ORDER BY observed_at_us DESC,measurement_id DESC LIMIT 1",
        (turn_id, invocation_id, provider, model),
    ).fetchone()
    return str(row[0]) if row is not None else None


def build_reconcile_plan(
    db_path: Path | str,
    turn_id: str,
    *,
    root: Path | str | None = None,
) -> ReconcilePlan:
    """Build deterministic spool events without mutating SQLite."""
    conn = connect(db_path, readonly=True)
    try:
        row, provider_session_id, lower_us, upper_us = _turn_context(
            conn, turn_id, Path(root) if root is not None else None
        )
        if not provider_session_id:
            raise ReconciliationError(
                f"turn {row['turn_id']} has no provider session correlation"
            )
        source_path, records = _select_records(
            conn, row, lower_us, upper_us, provider_session_id
        )
        claude_format = records[0].usage.measurement_source == "claude_transcript"
        invocation_rows = conn.execute(
            "SELECT invocation_id,invocation_index,provider_event_id,model_selected "
            "FROM llm_invocations WHERE turn_id=? ORDER BY invocation_index",
            (turn_id,),
        ).fetchall()
        existing_invocations = {
            str(item["provider_event_id"]): item
            for item in invocation_rows
            if item["provider_event_id"] is not None
        }
        uncorrelated_invocations = [
            item for item in invocation_rows
            if item["provider_event_id"] is None
        ]
        claimed_invocations: set[str] = set()
        provider = str(row["provider"])
        session_id = str(row["session_id"])
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session_row is None:
            raise ReconciliationError(f"session not found: {session_id}")
        events: list[MetricsEvent] = []
        measurement_ids: list[str] = []
        # A fresh producer per run: a rerun that selects more records must not
        # land on the sequence slots an earlier run already holds.
        producer_id = f"reconciler-{provider}-{turn_id}-{uuid7()}"
        sequence = 0
        events.append(MetricsEvent(
            event_type="session.recorded",
            producer_id=producer_id,
            producer_sequence=sequence,
            source="provider_reconciled",
            payload={
                **dict(session_row),
                "provider_session_id": provider_session_id,
            },
            event_id=deterministic_event_id(
                provider, f"{turn_id}:session-correlation:{_digest(dict(session_row))}"
            ),
            emitted_at_us=int(row["started_at_us"]),
            session_id=session_id,
            turn_id=turn_id,
        ))
        sequence += 1
        next_invocation_index = max(
            (int(value["invocation_index"]) for value in invocation_rows),
            default=0,
        )

        for index, record in enumerate(records, 1):
            provider_event_id = record.usage.provider_event_id
            existing = existing_invocations.get(provider_event_id)
            if existing is None:
                existing = next((
                    item for item in uncorrelated_invocations
                    if str(item["invocation_id"]) not in claimed_invocations
                    and str(item["model_selected"] or "") == record.usage.model
                ), None)
            invocation_id = (
                str(existing["invocation_id"])
                if existing is not None
                else (
                    None if not claude_format
                    else _stable_id("inv", turn_id, provider_event_id)
                )
            )
            if existing is not None:
                assert invocation_id is not None
                claimed_invocations.add(invocation_id)
                invocation_index = int(existing["invocation_index"])
            elif invocation_id is not None:
                next_invocation_index += 1
                invocation_index = next_invocation_index
            else:
                invocation_index = None
            if invocation_id is not None and (
                existing is None or existing["provider_event_id"] is None
            ):
                payload = {
                    "invocation_id": invocation_id,
                    "turn_id": turn_id,
                    "workflow_step_id": None,
                    "router_decision_id": None,
                    "parent_invocation_id": None,
                    "invocation_index": invocation_index,
                    "provider": provider,
                    "model_requested": None,
                    "model_selected": record.usage.model,
                    "provider_event_id": provider_event_id,
                    "provider_request_id": None,
                    "started_at_us": (
                        int(row["started_at_us"])
                        if index == 1 else records[index - 2].observed_at_us
                    ),
                    "first_token_at_us": None,
                    "ended_at_us": record.observed_at_us,
                    "wall_duration_ms": None,
                    "provider_latency_ms": None,
                    "time_to_first_token_ms": None,
                    "status": "completed",
                    "stop_reason": None,
                    "error_kind": None,
                }
                events.append(MetricsEvent(
                    event_type="invocation.recorded",
                    producer_id=producer_id,
                    producer_sequence=sequence,
                    source="provider_reconciled",
                    payload=payload,
                    event_id=deterministic_event_id(
                        provider, f"{turn_id}:invocation:{provider_event_id}"
                    ),
                    emitted_at_us=record.observed_at_us,
                    session_id=session_id,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                ))
            sequence += 1

            if claude_format:
                assert invocation_id is not None
                measurement_id = _stable_id(
                    "use", turn_id, invocation_id, provider_event_id,
                    record.usage.measurement_source,
                )
                payload = record.usage.to_measurement_payload(
                    measurement_id=measurement_id,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                    observed_at_us=record.observed_at_us,
                )
                payload["supersedes_measurement_id"] = _superseded_estimate(
                    conn,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                    provider=provider,
                    model=record.usage.model,
                )
                events.append(MetricsEvent(
                    event_type="usage.recorded",
                    producer_id=producer_id,
                    producer_sequence=sequence,
                    source="provider_reconciled",
                    payload=payload,
                    event_id=deterministic_event_id(
                        provider, f"{turn_id}:usage:{provider_event_id}"
                    ),
                    emitted_at_us=record.observed_at_us,
                    session_id=session_id,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                ))
                sequence += 1
                measurement_ids.append(measurement_id)

        aggregates = _aggregate(records) if claude_format else records
        for record in aggregates:
            measurement_id = _stable_id(
                "use", turn_id, "turn", record.usage.model,
                record.usage.measurement_source,
            )
            payload = record.usage.to_measurement_payload(
                measurement_id=measurement_id,
                turn_id=turn_id,
                observed_at_us=record.observed_at_us,
            )
            payload["supersedes_measurement_id"] = _superseded_estimate(
                conn,
                turn_id=turn_id,
                invocation_id=None,
                provider=provider,
                model=record.usage.model,
            )
            events.append(MetricsEvent(
                event_type="usage.recorded",
                producer_id=producer_id,
                producer_sequence=sequence,
                source="provider_reconciled",
                payload=payload,
                # Named by its content, so a fuller rerun is a new event that
                # updates the turn row instead of a conflict.
                event_id=deterministic_event_id(
                    provider, f"{turn_id}:turn-usage:{record.usage.model}:{_digest(payload)}"
                ),
                emitted_at_us=record.observed_at_us,
                session_id=session_id,
                turn_id=turn_id,
            ))
            sequence += 1
            measurement_ids.append(measurement_id)
    finally:
        conn.close()
    return ReconcilePlan(
        turn_id=turn_id,
        provider=provider,
        session_id=session_id,
        source_path=source_path,
        events=tuple(events),
        measurement_ids=tuple(measurement_ids),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _measurement_ids(db_path: Path, ids: tuple[str, ...]) -> set[str]:
    if not ids or not db_path.exists():
        return set()
    conn = connect(db_path, readonly=True)
    try:
        placeholders = ",".join("?" for _ in ids)
        return {
            str(row[0])
            for row in conn.execute(
                f"SELECT measurement_id FROM usage_measurements "
                f"WHERE measurement_id IN ({placeholders})",
                ids,
            )
        }
    finally:
        conn.close()


def reconcile_turn(
    root: Path | str,
    turn_id: str,
    *,
    wait: bool = False,
    timeout: float = 60.0,
    collect: bool = True,
) -> dict[str, object]:
    """Append exact recovery events and optionally wait for projection."""
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    started = time.monotonic()
    plan = build_reconcile_plan(db_path, turn_id, root=root)
    before = _measurement_ids(db_path, plan.measurement_ids)
    segment = (
        metrics_root
        / "spool"
        / f"reconciler-{os.getpid()}-{uuid7()}"
        / f"{time.time_ns()}-{uuid7()}.telemetry"
    )
    with SpoolWriter(segment, sync_every_frame=True) as writer:
        for event in plan.events:
            writer.append(event)

    collector_result: dict[str, object] = {"role": "deferred"}
    if collect:
        collector_result = MetricsCollector(db_path).collect_segment(segment)
    if wait and collect:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = _measurement_ids(db_path, plan.measurement_ids)
            if current == set(plan.measurement_ids):
                break
            time.sleep(0.05)
        else:
            raise ReconciliationError(
                f"reconciliation did not project within {timeout:.1f}s"
            )
    after = _measurement_ids(db_path, plan.measurement_ids)
    return {
        "status": "done" if after == set(plan.measurement_ids) else "pending",
        "turns_scanned": 1,
        "turns_repaired": int(bool(after - before)),
        "errors": 0,
        "turn_id": turn_id,
        "provider": plan.provider,
        "measurement_source": (
            next(event.payload["measurement_source"] for event in plan.events if event.event_type == "usage.recorded")
        ),
        "events_emitted": len(plan.events),
        "exact_measurements_recovered": len(after - before),
        "duplicates_skipped": len(before),
        "collector_role": collector_result["role"],
        "source_path": str(plan.source_path),
        "recovery_latency_ms": round((time.monotonic() - started) * 1000, 3),
    }


def reconcile_missing(
    root: Path | str,
    *,
    wait: bool = False,
    timeout: float = 60.0,
    collect: bool = True,
    terminal_only: bool = False,
    track_attempts: bool = False,
    budget_seconds: float | None = None,
) -> dict[str, object]:
    """Reconcile turns that currently have no exact turn measurement.

    ``track_attempts`` is the background pass, run by the collector that owns
    the database: failures are counted per turn in ``reconcile_attempts``, a
    turn with three is no longer a candidate, and success clears the count.
    ``budget_seconds`` ends a pass early; the next pass continues from there."""
    root = Path(root).resolve()
    db_path = root / "state" / "metrics" / "metrics.db"
    conn = connect(db_path, readonly=True)
    try:
        candidates = conn.execute(
            # A turn without an end that its own process has not moved on
            # from is over only once its writer exited (see _window_end).
            "SELECT t.turn_id, CASE WHEN t.ended_at_us IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM turns AS later WHERE later.session_id=t.session_id "
            "AND later.started_at_us>t.started_at_us) THEN 1 ELSE 0 END AS open_ended, "
            f"CASE WHEN t.ended_at_us IS NULL THEN ({_TURN_WRITER.format(turn='t.turn_id')}) "
            "END AS writer "
            "FROM turns AS t "
            "JOIN sessions AS s USING(session_id) "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM usage_measurements AS u "
            "WHERE u.turn_id=t.turn_id AND u.scope='turn' "
            "AND u.measurement_source IN ('claude_transcript', 'codex_rollout', 'provider_reconciled')) "
            "AND s.provider IN ('anthropic', 'openai', 'gateway') "
            + ("AND NOT EXISTS (SELECT 1 FROM reconcile_attempts AS r "
               "WHERE r.turn_id=t.turn_id AND r.attempts>=3) " if track_attempts else "")
            + "ORDER BY t.started_at_us",
        ).fetchall()
    finally:
        conn.close()
    # Turns still running are dropped before the budget starts, with one
    # liveness check per writer, so any number of them cannot hold back the
    # turns after them.
    writers: dict[str | None, bool] = {}
    turn_ids = []
    for turn_id, open_ended, writer in candidates:
        if terminal_only and open_ended:
            if writer not in writers:
                writers[writer] = _writer_gone(writer)
            if not writers[writer]:
                continue
        turn_ids.append(str(turn_id))
    started = time.monotonic()
    results = []
    failures = []
    succeeded: list[str] = []
    for turn_id in turn_ids:
        if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
            break
        try:
            results.append(
                reconcile_turn(
                    root,
                    turn_id,
                    wait=wait,
                    timeout=timeout,
                    collect=collect,
                )
            )
            succeeded.append(turn_id)
        except TurnStillRunning as exc:
            if not terminal_only:
                failures.append({"turn_id": turn_id, "error": str(exc)})
        except (ReconciliationError, ProviderContractError, OSError) as exc:
            failures.append({"turn_id": turn_id, "error": str(exc)})
    exhausted = 0
    if track_attempts:
        exhausted = _record_attempts(db_path, [item["turn_id"] for item in failures], succeeded)
    return {
        "status": "done" if not failures and not exhausted else "partial",
        "turns_scanned": len(turn_ids),
        "turns_repaired": sum(
            int(bool(result["exact_measurements_recovered"]))
            for result in results
        ),
        "errors": len(failures),
        "retry_exhausted_turns": exhausted,
        "turns_considered": len(results) + len(failures),
        "turns_reconciled": len(results),
        "exact_measurements_recovered": sum(
            int(result["exact_measurements_recovered"]) for result in results
        ),
        "failures": failures,
        "results": results,
    }


def _record_attempts(db_path: Path, failed: list[str], succeeded: list[str]) -> int:
    """Count this pass's failures, clear its successes; returns the turns now exhausted."""
    now_us = time.time_ns() // 1000
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO reconcile_attempts(turn_id, attempts, last_error_at_us) VALUES(?, 1, ?) "
                "ON CONFLICT(turn_id) DO UPDATE SET attempts=attempts+1, "
                "last_error_at_us=excluded.last_error_at_us",
                [(turn_id, now_us) for turn_id in failed],
            )
            conn.executemany("DELETE FROM reconcile_attempts WHERE turn_id=?",
                             [(turn_id,) for turn_id in succeeded])
            # A turn recovered another way (a manual run) is no longer owed a retry.
            conn.execute(
                "DELETE FROM reconcile_attempts WHERE turn_id IN (SELECT u.turn_id FROM usage_measurements u "
                "WHERE u.scope='turn' AND u.measurement_source IN "
                "('claude_transcript', 'codex_rollout', 'provider_reconciled'))")
        return int(conn.execute("SELECT COUNT(*) FROM reconcile_attempts WHERE attempts>=3").fetchone()[0])
    finally:
        conn.close()
