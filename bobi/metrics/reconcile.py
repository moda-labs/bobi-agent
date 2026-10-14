"""Out-of-band exact usage recovery from provider transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from bobi.chat_history import find_claude_transcript, find_codex_rollout
from bobi.fsutil import try_file_lock
from bobi.metrics.collector import (
    MetricsCollector,
    MetricsCollectorService,
    imported_through,
    spool_sizes,
    writer_finished,
)
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
    """Evidence about the turn may still be on its way: its transcript may
    grow, or rows that bound or describe it are not imported or projected yet.
    A recovery now could be partial or take another turn's calls. *hint* says
    what an operator can do about it."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# How long a manual reconcile waits for a running collector to import what
# was spooled before it started; a background pass holds imports for at most
# its budget.
_IMPORT_WAIT_SECONDS = 15.0


def _await_import(root: Path, db_path: Path, sizes: dict[Path, int] | None = None) -> None:
    """Return once every frame runtime writers spooled before this call (or
    up to *sizes*, see spool_sizes) is imported, so no turn is decided while a
    row that bounds it (the next turn's start) is still in the spool.

    With no collector running, this takes the collector lock and runs the
    collector's own import; while a collector holds the lock, it waits for
    that collector, up to _IMPORT_WAIT_SECONDS."""
    if sizes is None:
        sizes = spool_sizes(_spool_root(db_path))
    if not sizes or not db_path.exists():
        return
    deadline = time.monotonic() + _IMPORT_WAIT_SECONDS
    service = MetricsCollectorService(root)
    while True:
        conn = connect(db_path, readonly=True)
        try:
            if imported_through(conn, sizes):
                return
        finally:
            conn.close()
        with try_file_lock(service.collector.lock_path) as acquired:
            if acquired:
                service.import_and_save_locked()
                return
        if time.monotonic() >= deadline:
            raise TurnStillRunning(
                "the running collector has not imported the spooled metrics events "
                f"within {_IMPORT_WAIT_SECONDS:.0f}s; reconcile once it has"
            )
        time.sleep(0.05)


# A turn's start is stamped just before its producer queues it for the spool;
# this bounds that lag, so a row stamped this long before an import is in it.
_SPOOL_LAG_US = 2_000_000


def _imported() -> None:
    """The import_spool of a caller whose _ImportGate already ran."""


class _ImportGate:
    """What reconcile_missing runs before each turn, so the turn is decided
    from imported rows without importing (or listing the spool) per turn.

    - A window that ends (at a known next turn's start, or five seconds after
      the turn's end) well before the last import started is decided by rows
      that import held: a turn starting in it was spooled before.
    - A turn without an end whose writer was seen gone before the last import
      started (*confirmed*) has nothing of its own stamped after that import;
      a next turn the import did not see started after it. Its window ends at
      that import instead of "now", so no import is needed either.
    - Any other turn gets a fresh import, run only when a runtime writer's
      segment changed since the last one.
    """

    def __init__(self, root: Path, db_path: Path, import_spool: Callable[[], object] | None) -> None:
        self.root = root
        self.db_path = db_path
        self.import_spool = import_spool
        self.imported_at_us = 0
        self.sizes: dict[Path, int] | None = None
        self.confirmed: set[str] = set()
        # One read-only connection for the run; with no open transaction each
        # query sees what the imports since committed.
        self._conn = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def sync(self) -> None:
        started = time.time_ns() // 1000
        sizes = spool_sizes(_spool_root(self.db_path))
        if sizes != self.sizes:
            if self.import_spool is not None:
                self.import_spool()
            else:
                _await_import(self.root, self.db_path, sizes)
            self.sizes = sizes
        self.imported_at_us = started

    def before(self, turn_id: str) -> int | None:
        """Make the turn decidable; returns the time that stands in for "now"
        in its window, or None for the current time."""
        if self._conn is None:
            self._conn = connect(self.db_path, readonly=True)
        if _window_settled(self._conn, turn_id, self.imported_at_us - _SPOOL_LAG_US):
            return None
        if turn_id in self.confirmed:
            return self.imported_at_us
        self.sync()
        return None


def _window_settled(conn, turn_id: str, horizon_us: int) -> bool:
    """True when the turn's window ends no later than *horizon_us* whatever
    else is spooled: at a known next turn's start, or at its end plus slack."""
    row = conn.execute(
        f"SELECT {_TURN_COLUMNS} FROM turns AS t JOIN sessions AS s USING(session_id) "
        "WHERE t.turn_id=?",
        (turn_id,),
    ).fetchone()
    if row is None:
        return False
    ends = [int(turn["started_at_us"]) for turn in _adjacent_turn(
        conn, row, str(row["provider_session_id"] or ""), later=True) if turn]
    if row["ended_at_us"] is not None:
        ends.append(int(row["ended_at_us"]) + _END_SLACK_US)
    return bool(ends) and min(ends) <= horizon_us


def _require_projected(conn, turn_id: str) -> None:
    """Refuse a turn whose own events are imported but not projected: its
    calls usually wait for their workflow step's row, written when the step
    ends. Recovering it now would record its calls a second time, without
    their step, and leave its tool rows without a parent. An event that never
    finds its parent is quarantined after ORPHAN_TTL_US, which ends the wait."""
    waiting, reason = conn.execute(
        "SELECT COUNT(*), MIN(projection_error) FROM raw_events "
        "WHERE projection_state='pending' AND turn_id=?",
        (turn_id,),
    ).fetchone()
    if waiting:
        raise TurnStillRunning(
            f"turn {turn_id} has {waiting} event(s) waiting to be projected "
            f"({reason or 'not projected yet'}); reconcile it once they project",
            hint="Rerun with --force to reconcile it now; its own calls then lose "
            "their workflow step and tool rows.",
        )


# The runtime producer that recorded a turn; served by the partial index
# idx_raw_turn_writer, not a raw_events scan.
_TURN_WRITER = (
    "SELECT producer_id FROM raw_events WHERE event_type='turn.recorded' "
    "AND turn_id={turn} AND producer_id LIKE 'producer-%' LIMIT 1"
)
_TURN_WRITER_SQL = _TURN_WRITER.format(turn="?")


def _writer_done(conn, spool_root: Path, writer: str | None, *, force: bool = False) -> bool:
    """True once the turn's writer is gone and all it spooled is imported
    (see writer_finished). A turn whose writer cannot be found is not taken
    as abandoned unless *force* says so; *force* never skips the import."""
    if writer is None:
        return force
    return writer_finished(conn, spool_root, writer, assume_exited=force)


def _require_writer_done(conn, spool_root: Path, turn_id: str, *, force: bool) -> None:
    row = conn.execute(_TURN_WRITER_SQL, (turn_id,)).fetchone()
    writer = str(row[0]) if row is not None else None
    if _writer_done(conn, spool_root, writer, force=force):
        return
    if force:
        raise TurnStillRunning(
            f"turn {turn_id} has spooled events that are not imported yet; "
            "reconcile it once the collector has imported them"
        )
    raise TurnStillRunning(
        f"turn {turn_id} is still running; reconcile it after it ends",
        hint="If its process is known to have exited, rerun with --force.",
    )


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


def _window_end(conn, row, provider_session_id: str, spool_root: Path, *,
                force: bool = False, now_us: int | None = None) -> int:
    """The latest time a provider record can belong to the turn.

    A turn ends at the next turn of its metrics session or of its provider
    session. A turn without an end is over once its own process moved on, or
    once the process that recorded it is done (writer_finished); a later turn
    of another process does not end it while its writer still runs. *force*
    takes the writer as gone. *now_us* stands in for the current time (see
    _ImportGate).
    """
    same_session, other_session = _adjacent_turn(conn, row, provider_session_id, later=True)
    next_starts = [int(turn["started_at_us"]) for turn in (same_session, other_session) if turn]
    if row["ended_at_us"] is not None:
        return min([int(row["ended_at_us"]) + _END_SLACK_US, *next_starts])
    if same_session is None:
        _require_writer_done(conn, spool_root, str(row["turn_id"]), force=force)
    # A turn whose process died never records its end; its transcript holds
    # nothing later than now.
    return min([time.time_ns() // 1000 if now_us is None else now_us, *next_starts])


def _window_start(conn, row, provider_session_id: str, spool_root: Path, *,
                  now_us: int | None = None) -> int:
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
        previous_end = _window_end(conn, previous, provider_session_id, spool_root, now_us=now_us)
    except TurnStillRunning:
        # Another live process on the provider session: unsupported, so take
        # nothing from before this turn's own start.
        previous_end = started - 1
    return max(lower, previous_end + 1)


def _turn_context(conn, turn_id: str, root: Path | None, spool_root: Path, *,
                  force: bool = False, now_us: int | None = None):
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
    upper = _window_end(conn, row, provider_session_id, spool_root, force=force, now_us=now_us)
    lower = _window_start(conn, row, provider_session_id, spool_root, now_us=now_us)
    return row, provider_session_id, lower, upper


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
    force: bool = False,
    now_us: int | None = None,
) -> ReconcilePlan:
    """Build deterministic spool events without mutating SQLite.

    *force* takes the writer of a turn without an end as gone, and reconciles
    a turn whose own events still wait to be projected; the writer's spool
    must still be imported."""
    conn = connect(db_path, readonly=True)
    try:
        row, provider_session_id, lower_us, upper_us = _turn_context(
            conn, turn_id, Path(root) if root is not None else None,
            _spool_root(db_path), force=force, now_us=now_us,
        )
        if not force:
            _require_projected(conn, turn_id)
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


def _spool_root(db_path: Path | str) -> Path:
    return Path(db_path).parent / "spool"


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
    force: bool = False,
    import_spool: Callable[[], object] | None = None,
    now_us: int | None = None,
) -> dict[str, object]:
    """Append exact recovery events and optionally wait for projection.

    *force* reconciles a turn without an end whose writer still looks alive,
    for an operator who knows its process is gone. On a turn that is really
    running it records a partial turn that collides with the writer's own
    events, so it is never implied by naming the turn.

    The turn is decided only from rows imported after this call starts: the
    caller holding the collector lock passes *import_spool*; anyone else
    imports or waits for the running collector (see _await_import). A record a later turn
    wrote to the transcript by then comes from a request that turn sent after
    spooling its start, so that start bounds this turn's window."""
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    db_path = metrics_root / "metrics.db"
    started = time.monotonic()
    if import_spool is not None:
        import_spool()
    else:
        _await_import(root, db_path)
    plan = build_reconcile_plan(db_path, turn_id, root=root, force=force, now_us=now_us)
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
        "forced": force,
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
    import_spool: Callable[[], object] | None = None,
) -> dict[str, object]:
    """Reconcile turns that currently have no exact turn measurement.

    ``track_attempts`` is the background pass, run by the collector that owns
    the database: failures are counted per turn in ``reconcile_attempts``, a
    turn with three is no longer a candidate, and success clears the count.
    ``budget_seconds`` ends a pass early; the next pass continues from there.
    ``import_spool`` is the import of a caller holding the collector lock;
    without it the run imports or waits as reconcile_turn does. It runs once
    before the candidates are read, then before a turn only when that turn's
    window may end at a row the last import did not see (see _ImportGate).
    With ``terminal_only``, a turn whose evidence is still on its way is
    skipped without counting an attempt, and counted in ``turns_waiting``."""
    root = Path(root).resolve()
    db_path = root / "state" / "metrics" / "metrics.db"
    # The candidates are read from the database: a turn still in the spool
    # would not be one.
    gate = _ImportGate(root, db_path, import_spool)
    gate.sync()
    conn = connect(db_path, readonly=True)
    try:
        candidates = conn.execute(
            # A turn without an end that its own process has not moved on
            # from is over only once its writer is done (see _window_end).
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
        # Turns still running are dropped before the budget starts, with one
        # writer_finished check per writer, so any number of them cannot hold
        # back the turns after them.
        spool_root = _spool_root(db_path)
        writers: dict[str | None, bool] = {}
        turn_ids = []
        for turn_id, open_ended, writer in candidates:
            if terminal_only and open_ended:
                if writer not in writers:
                    writers[writer] = _writer_done(conn, spool_root, writer)
                if not writers[writer]:
                    continue
                gate.confirmed.add(str(turn_id))
            turn_ids.append(str(turn_id))
    finally:
        conn.close()
    # After the liveness checks, so the confirmed writers were gone before it.
    gate.sync()
    started = time.monotonic()
    results = []
    failures = []
    waiting = 0
    succeeded: list[str] = []
    try:
        for turn_id in turn_ids:
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                break
            try:
                now_us = gate.before(turn_id)
                results.append(
                    reconcile_turn(
                        root,
                        turn_id,
                        wait=wait,
                        timeout=timeout,
                        collect=collect,
                        import_spool=_imported,
                        now_us=now_us,
                    )
                )
                succeeded.append(turn_id)
            except TurnStillRunning as exc:
                if terminal_only:
                    waiting += 1
                else:
                    failures.append({"turn_id": turn_id, "error": str(exc)})
            except (ReconciliationError, ProviderContractError, OSError) as exc:
                failures.append({"turn_id": turn_id, "error": str(exc)})
    finally:
        gate.close()
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
        "turns_waiting": waiting,
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
