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
from bobi.metrics.collector import MetricsCollector
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


def _turn_context(conn, turn_id: str):
    row = conn.execute(
        "SELECT t.*,s.provider,s.brain,s.provider_session_id,s.session_name "
        "FROM turns AS t JOIN sessions AS s USING(session_id) "
        "WHERE t.turn_id=?",
        (turn_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationError(f"turn not found: {turn_id}")
    upper = row["ended_at_us"]
    if upper is None:
        next_turn = conn.execute(
            "SELECT started_at_us FROM turns WHERE session_id=? "
            "AND started_at_us>? ORDER BY started_at_us LIMIT 1",
            (row["session_id"], row["started_at_us"]),
        ).fetchone()
        upper = next_turn[0] if next_turn is not None else time.time_ns() // 1000
    return row, int(upper)


def _select_records(
    conn,
    row,
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
        records = parse_claude_transcript_records(source)
    else:
        source = codex_source
        if source is None:
            raise ReconciliationError(
                f"Codex rollout not found for {provider_session_id}"
            )
        records = parse_codex_rollout_records(source)
    records = [replace(record, usage=replace(record.usage, provider=provider)) for record in records]

    used_elsewhere = {
        str(item[0])
        for item in conn.execute(
            "SELECT provider_event_id FROM usage_measurements "
            "WHERE provider=? AND turn_id<>? AND provider_event_id IS NOT NULL",
            (provider, row["turn_id"]),
        )
    }
    lower_us = int(row["started_at_us"]) - 2_000_000
    upper_us += 5_000_000
    provider_turn_id = str(row["provider_turn_id"] or "")
    selected = [
        record
        for record in records
        if lower_us <= record.observed_at_us <= upper_us
        and record.usage.provider_event_id not in used_elsewhere
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
        row, upper_us = _turn_context(conn, turn_id)
        provider_session_id = str(row["provider_session_id"] or "")
        if not provider_session_id and root is not None:
            provider_session_id = load_session_id(
                str(row["session_name"]), root=Path(root)
            )
        if not provider_session_id:
            raise ReconciliationError(
                f"turn {row['turn_id']} has no provider session correlation"
            )
        source_path, records = _select_records(
            conn, row, upper_us, provider_session_id
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
        producer_id = f"reconciler-{provider}-{turn_id}"
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
                provider, f"{turn_id}:session-correlation"
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
                event_id=deterministic_event_id(
                    provider, f"{turn_id}:turn-usage:{record.usage.model}"
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
    retry_attempts: dict[str, int] | None = None,
) -> dict[str, object]:
    """Reconcile turns that currently have no exact turn measurement."""
    root = Path(root).resolve()
    db_path = root / "state" / "metrics" / "metrics.db"
    conn = connect(db_path, readonly=True)
    try:
        turn_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT t.turn_id FROM turns AS t "
                "JOIN sessions AS s USING(session_id) "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM usage_measurements AS u "
                "WHERE u.turn_id=t.turn_id AND u.scope='turn' "
                "AND u.measurement_source IN ('claude_transcript', 'codex_rollout', 'provider_reconciled')) "
                "AND s.provider IN ('anthropic', 'openai', 'gateway') "
                + ("AND t.ended_at_us IS NOT NULL " if terminal_only else "")
                + "ORDER BY t.started_at_us"
            )
        ]
    finally:
        conn.close()
    if retry_attempts is not None:
        for turn_id in set(retry_attempts) - set(turn_ids):
            del retry_attempts[turn_id]
    results = []
    failures = []
    exhausted = 0
    for turn_id in turn_ids:
        # ponytail: three background retries; manual reconciliation handles late transcripts.
        if retry_attempts is not None and retry_attempts.get(turn_id, 0) >= 3:
            exhausted += 1
            continue
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
        except (ReconciliationError, ProviderContractError, OSError) as exc:
            if retry_attempts is not None:
                retry_attempts[turn_id] = retry_attempts.get(turn_id, 0) + 1
            failures.append({"turn_id": turn_id, "error": str(exc)})
    return {
        "status": "done" if not failures and not exhausted else "partial",
        "turns_scanned": len(turn_ids),
        "turns_repaired": sum(
            int(bool(result["exact_measurements_recovered"]))
            for result in results
        ),
        "errors": len(failures),
        "retry_exhausted_turns": exhausted,
        "turns_considered": len(turn_ids),
        "turns_reconciled": len(results),
        "exact_measurements_recovered": sum(
            int(result["exact_measurements_recovered"]) for result in results
        ),
        "failures": failures,
        "results": results,
    }
