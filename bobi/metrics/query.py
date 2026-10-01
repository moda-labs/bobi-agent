"""Bounded, privacy-safe read models for metrics Admin and MCP queries."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bobi.metrics.schema import SCHEMA_VERSION
from bobi.metrics.experiment import sample_ratio_mismatch
from bobi.metrics.store import connect

MAX_ROWS = 200
DEFAULT_ROWS = 50
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RANGE_SECONDS = 31 * 24 * 60 * 60
QUERY_DEADLINE_SECONDS = 1.0

TOKEN_COLUMNS = (
    "input_tokens",
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "cache_write_5m_input_tokens",
    "cache_write_1h_input_tokens",
    "cache_write_unknown_ttl_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

SESSION_PUBLIC_COLUMNS = (
    "session_id", "session_name", "provider_session_id", "brain", "provider",
    "role", "run_key", "workflow_name", "started_at_us",
    "ended_at_us", "status", "terminal_error_kind", "parent_session_id",
)

TURN_PUBLIC_COLUMNS = (
    "turn_id", "session_id", "turn_index", "trigger_kind", "trigger_id",
    "is_user_initiated", "prompt_template_id", "prompt_template_version",
    "prompt_bytes", "started_at_us", "first_output_at_us",
    "ended_at_us", "wall_duration_ms", "provider_duration_ms",
    "provider_api_duration_ms", "status", "error_kind", "provider_turn_id",
)

WORKFLOW_STEP_PUBLIC_COLUMNS = (
    "workflow_step_id", "session_id", "turn_id", "run_key", "workflow_name",
    "step_name", "step_index", "attempt", "step_type", "started_at_us",
    "ended_at_us", "status", "error_kind",
)

INVOCATION_PUBLIC_COLUMNS = (
    "invocation_id", "turn_id", "workflow_step_id", "router_decision_id",
    "parent_invocation_id", "invocation_index", "provider", "model_requested",
    "model_selected", "provider_event_id", "provider_request_id", "started_at_us",
    "first_token_at_us", "ended_at_us", "wall_duration_ms",
    "provider_latency_ms", "time_to_first_token_ms", "status", "stop_reason",
    "error_kind",
)


def _columns(columns: tuple[str, ...]) -> str:
    return ",".join(columns)


def _strict_sum(column: str) -> str:
    """Aggregate a provider dimension without turning partial knowledge into fact."""
    return f"CASE WHEN COUNT({column})=COUNT(*) THEN SUM({column}) END AS {column}"


def _complete_exact_invocation_usage(alias: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        f"AND i.model_selected={alias}.model) "
        f"AND NOT EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        f"AND i.model_selected={alias}.model "
        "AND NOT EXISTS (SELECT 1 FROM best_usage measured "
        "WHERE measured.scope='invocation' AND measured.invocation_id=i.invocation_id "
        f"AND measured.model={alias}.model AND measured.is_estimated=0))"
    )


def _complete_effective_invocation_usage(alias: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        f"AND i.model_selected={alias}.model) "
        f"AND NOT EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        f"AND i.model_selected={alias}.model "
        "AND NOT EXISTS (SELECT 1 FROM best_usage measured "
        "WHERE measured.scope='invocation' AND measured.invocation_id=i.invocation_id "
        f"AND measured.model={alias}.model))"
    )


def _exact_turn_usage_exists(alias: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM best_usage turn_exact "
        f"WHERE turn_exact.scope='turn' AND turn_exact.turn_id={alias}.turn_id "
        f"AND turn_exact.model={alias}.model AND turn_exact.is_estimated=0)"
    )


def _single_invocation_model_has_exact_turn(alias: str) -> str:
    """Treat a sole invocation/terminal model mismatch as a provider alias."""
    return (
        f"((SELECT COUNT(DISTINCT i.model_selected) FROM llm_invocations i "
        f"WHERE i.turn_id={alias}.turn_id)=1 AND EXISTS ("
        "SELECT 1 FROM best_usage turn_exact "
        f"WHERE turn_exact.scope='turn' AND turn_exact.turn_id={alias}.turn_id "
        "AND turn_exact.is_estimated=0))"
    )


def _effective_usage(alias: str) -> str:
    return (
        f"({alias}.is_estimated=0 OR NOT EXISTS (SELECT 1 FROM best_usage exact "
        f"WHERE exact.scope={alias}.scope AND exact.turn_id={alias}.turn_id "
        f"AND COALESCE(exact.invocation_id,'')=COALESCE({alias}.invocation_id,'') "
        "AND exact.is_estimated=0))"
    )


def _chosen_usage(alias: str = "b") -> str:
    exact_invocations = _complete_exact_invocation_usage(alias)
    effective_invocations = _complete_effective_invocation_usage(alias)
    exact_turn = _exact_turn_usage_exists(alias)
    aliased_terminal_turn = _single_invocation_model_has_exact_turn(alias)
    return (
        f"({_effective_usage(alias)} AND ("
        f"({alias}.scope='invocation' AND NOT ({exact_turn}) "
        f"AND NOT ({aliased_terminal_turn}) AND "
        f"(({exact_invocations}) OR ({effective_invocations}))) OR "
        f"({alias}.scope='turn' AND (({exact_turn}) OR "
        f"(NOT ({exact_invocations}) AND NOT ({effective_invocations}))))"
        "))"
    )


def _complete_exact_invocation_cost(alias: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id) "
        f"AND NOT EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        "AND NOT EXISTS (SELECT 1 FROM cost_measurements measured "
        "WHERE measured.scope='invocation' AND measured.invocation_id=i.invocation_id "
        "AND measured.is_estimated=0))"
    )


def _complete_effective_invocation_cost(alias: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id) "
        f"AND NOT EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id={alias}.turn_id "
        "AND NOT EXISTS (SELECT 1 FROM cost_measurements measured "
        "WHERE measured.scope='invocation' AND measured.invocation_id=i.invocation_id "
        f"AND {_effective_cost('measured')}))"
    )


def _exact_turn_cost_exists(alias: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM cost_measurements turn_exact "
        f"WHERE turn_exact.scope='turn' AND turn_exact.turn_id={alias}.turn_id "
        "AND turn_exact.is_estimated=0)"
    )


def _effective_cost(alias: str) -> str:
    return (
        f"({alias}.is_estimated=0 OR NOT EXISTS (SELECT 1 FROM cost_measurements exact "
        f"WHERE exact.scope={alias}.scope "
        f"AND COALESCE(exact.session_id,'')=COALESCE({alias}.session_id,'') "
        f"AND COALESCE(exact.turn_id,'')=COALESCE({alias}.turn_id,'') "
        f"AND COALESCE(exact.invocation_id,'')=COALESCE({alias}.invocation_id,'') "
        "AND exact.is_estimated=0))"
    )


def _chosen_cost(alias: str = "c") -> str:
    exact_invocations = _complete_exact_invocation_cost(alias)
    effective_invocations = _complete_effective_invocation_cost(alias)
    exact_turn = _exact_turn_cost_exists(alias)
    return (
        f"({_effective_cost(alias)} AND ("
        f"({alias}.scope='invocation' AND (({exact_invocations}) OR "
        f"(NOT ({exact_turn}) AND ({effective_invocations})))) OR "
        f"({alias}.scope='turn' AND NOT ({exact_invocations}) AND "
        f"(({exact_turn}) OR NOT ({effective_invocations})))"
        "))"
    )


class MetricsQueryError(Exception):
    """Expected query refusal with a stable Admin protocol code."""

    def __init__(self, message: str, code: str, **detail: object) -> None:
        self.code = code
        self.detail = {key: value for key, value in detail.items() if value is not None}
        super().__init__(message)


def _row(row: sqlite3.Row | None, *, omit: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys() if key not in omit}


def _required_id(args: dict[str, Any], name: str) -> str:
    raw = args.get(name)
    if not isinstance(raw, str):
        raise MetricsQueryError(f"{name} must be a string", "bad_request")
    value = raw.strip()
    if not value:
        raise MetricsQueryError(f"{name} is required", "bad_request")
    return value


def _limit(args: dict[str, Any], name: str = "limit", default: int = DEFAULT_ROWS) -> int:
    raw = args.get(name, default)
    if isinstance(raw, bool):
        raise MetricsQueryError(f"{name} must be an integer", "bad_request")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise MetricsQueryError(f"{name} must be an integer", "bad_request") from None
    if value < 1 or value > MAX_ROWS:
        raise MetricsQueryError(f"{name} must be between 1 and {MAX_ROWS}", "query_too_large")
    return value


def _timestamp(value: object, name: str) -> float:
    if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc).timestamp()
        except ValueError:
            pass
    raise MetricsQueryError(f"{name} must be an ISO-8601 or epoch timestamp", "invalid_range")


def _time_range(args: dict[str, Any], *, now: float | None = None) -> tuple[int, int]:
    now = time.time() if now is None else now
    explicit = args.get("from") is not None or args.get("to") is not None
    legacy = args.get("window_seconds") is not None or args.get("end_at") is not None
    if explicit and legacy:
        raise MetricsQueryError("use either from/to or window_seconds/end_at", "invalid_range")
    if explicit:
        if args.get("from") is None or args.get("to") is None:
            raise MetricsQueryError("from and to must be supplied together", "invalid_range")
        start, end = _timestamp(args["from"], "from"), _timestamp(args["to"], "to")
    else:
        raw_window = args.get("window_seconds", 24 * 60 * 60)
        if isinstance(raw_window, bool):
            raise MetricsQueryError("window_seconds must be positive", "invalid_range")
        try:
            window = float(raw_window)
        except (TypeError, ValueError):
            raise MetricsQueryError("window_seconds must be positive", "invalid_range") from None
        if not math.isfinite(window) or window <= 0:
            raise MetricsQueryError("window_seconds must be positive", "invalid_range")
        end = now if args.get("end_at") is None else _timestamp(args["end_at"], "end_at")
        start = end - window
    if end <= start or end - start > MAX_RANGE_SECONDS:
        raise MetricsQueryError("time range must be positive and at most 31 days", "invalid_range")
    return int(start * 1_000_000), int(end * 1_000_000)


def _filter_digest(filters: dict[str, object]) -> str:
    raw = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _encode_cursor(position: dict[str, object], filters: dict[str, object]) -> str:
    raw = json.dumps(
        {"position": position, "filter": _filter_digest(filters)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    value: object,
    filters: dict[str, object],
    fields: tuple[str, ...],
) -> dict[str, object] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MetricsQueryError(
            "cursor does not match this query", "invalid_cursor",
        )
    try:
        data = json.loads(
            base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)),
        )
        position = data["position"]
        if (
            not isinstance(position, dict)
            or set(position) != set(fields)
            or data["filter"] != _filter_digest(filters)
        ):
            raise ValueError
        return position
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise MetricsQueryError("cursor does not match this query", "invalid_cursor") from None


def _coverage(conn: sqlite3.Connection, where: str = "", params: tuple[object, ...] = ()) -> dict[str, object]:
    clause = f" WHERE {where}" if where else ""
    measured = conn.execute(
        "WITH filtered AS (SELECT i.invocation_id FROM llm_invocations i" + clause + "),"
        "coverage AS (SELECT f.invocation_id,"
        "EXISTS (SELECT 1 FROM usage_measurements u WHERE u.invocation_id=f.invocation_id "
        "AND u.scope='invocation' AND u.is_estimated=0 AND NOT EXISTS "
        "(SELECT 1 FROM usage_measurements newer WHERE newer.supersedes_measurement_id=u.measurement_id)) AS exact,"
        "EXISTS (SELECT 1 FROM usage_measurements u WHERE u.invocation_id=f.invocation_id "
        "AND u.scope='invocation' AND u.is_estimated=1 AND NOT EXISTS "
        "(SELECT 1 FROM usage_measurements newer WHERE newer.supersedes_measurement_id=u.measurement_id)) AS estimated "
        "FROM filtered f) SELECT COUNT(*),COALESCE(SUM(exact),0),"
        "COALESCE(SUM(CASE WHEN exact=0 THEN estimated ELSE 0 END),0) FROM coverage",
        params,
    ).fetchone()
    total, exact, estimated = (int(measured[0]), int(measured[1]), int(measured[2]))
    retained = conn.execute("SELECT MIN(started_at_us),MAX(COALESCE(ended_at_us,started_at_us)) FROM turns").fetchone()
    return {
        "exact_invocations": exact,
        "estimated_invocations": estimated,
        "unknown_invocations": max(0, total - exact - estimated),
        "events_dropped": None,
        "collector_lag_ms": None,
        "retained_from": retained[0],
        "retained_to": retained[1],
    }


def _bounded(payload: dict[str, object]) -> dict[str, object]:
    if len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()) > MAX_RESPONSE_BYTES:
        raise MetricsQueryError("response exceeds 512 KiB; narrow the query", "query_too_large")
    return payload

def _enforce_row_count(*collections: list[object]) -> None:
    if sum(len(collection) for collection in collections) > MAX_ROWS:
        raise MetricsQueryError("response exceeds 200 rows; narrow the query", "query_too_large")


def _usage_granularity(invocation_turns: int, turn_turns: int) -> str:
    if invocation_turns and turn_turns:
        return "mixed"
    if invocation_turns:
        return "invocation"
    if turn_turns:
        return "turn"
    return "none"


def _with_usage_granularity(
    coverage: dict[str, object],
    invocation_turns: object,
    turn_turns: object,
) -> dict[str, object]:
    invocation_count = int(invocation_turns or 0)
    turn_count = int(turn_turns or 0)
    return {
        **coverage,
        "usage_granularity": _usage_granularity(invocation_count, turn_count),
        "invocation_granularity_turns": invocation_count,
        "turn_granularity_turns": turn_count,
    }


class MetricsQueries:
    """Open one read-only SQLite snapshot per bounded query."""

    def __init__(self, root: Path | str, *, deadline_seconds: float = QUERY_DEADLINE_SECONDS) -> None:
        self.db_path = Path(root).resolve() / "state" / "metrics" / "metrics.db"
        self.deadline_seconds = deadline_seconds

    def _run(self, build: Callable[[sqlite3.Connection], dict[str, object]]) -> dict[str, object]:
        if not self.db_path.is_file():
            raise MetricsQueryError("metrics database is not ready", "metrics_not_ready", retry_after_ms=1000)
        try:
            conn = connect(self.db_path, readonly=True)
        except sqlite3.Error as exc:
            raise MetricsQueryError("metrics database is unavailable", "metrics_not_ready", retry_after_ms=1000) from exc
        deadline = time.monotonic() + self.deadline_seconds
        conn.execute("PRAGMA busy_timeout = 50")
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            payload = _bounded(build(conn))
            if time.monotonic() >= deadline:
                raise MetricsQueryError("metrics query exceeded its deadline", "metrics_busy", retry_after_ms=250)
            return payload
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupted" in message:
                raise MetricsQueryError("metrics query exceeded its one-second deadline", "metrics_busy", retry_after_ms=250) from exc
            if "locked" in message or "busy" in message:
                raise MetricsQueryError("metrics database is busy", "metrics_busy", retry_after_ms=250) from exc
            raise MetricsQueryError("metrics database is unavailable", "metrics_not_ready", retry_after_ms=1000) from exc
        finally:
            conn.close()

    def summary(self, args: dict[str, Any]) -> dict[str, object]:
        start, end = _time_range(args)
        session_id = str(args.get("session_id") or args.get("session") or "").strip() or None
        model = str(args.get("model") or "").strip() or None
        run_key = str(args.get("run_key") or "").strip() or None
        experiment_id = str(args.get("experiment_id") or "").strip() or None
        variant_id = str(args.get("variant_id") or "").strip() or None
        raw_group_by = args.get("group_by") or []
        if not isinstance(raw_group_by, list):
            raise MetricsQueryError("group_by must be a list", "bad_request")
        group_columns = {
            "provider": "b.provider",
            "model": "b.model",
            "session_id": "t.session_id",
            "prompt_template_id": "t.prompt_template_id",
            "experiment_id": "(SELECT r.experiment_id FROM router_decisions r WHERE r.turn_id=t.turn_id ORDER BY r.decided_at_us LIMIT 1)",
            "variant_id": "(SELECT r.variant_id FROM router_decisions r WHERE r.turn_id=t.turn_id ORDER BY r.decided_at_us LIMIT 1)",
        }
        group_by = [str(item) for item in raw_group_by]
        if len(group_by) != len(set(group_by)) or any(item not in group_columns for item in group_by):
            raise MetricsQueryError("unsupported or duplicate group_by dimension", "bad_request")

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            filters = ["t.started_at_us>=?", "t.started_at_us<?"]
            params: list[object] = [start, end]
            if session_id:
                filters.append("t.session_id=?")
                params.append(session_id)
            if model:
                filters.append("b.model=?")
                params.append(model)
            if run_key:
                filters.append("s.run_key=?")
                params.append(run_key)
            if experiment_id:
                filters.append("EXISTS (SELECT 1 FROM router_decisions r WHERE r.turn_id=t.turn_id AND r.experiment_id=?)")
                params.append(experiment_id)
            if variant_id:
                filters.append("EXISTS (SELECT 1 FROM router_decisions r WHERE r.turn_id=t.turn_id AND r.variant_id=?)")
                params.append(variant_id)
            where = " AND ".join(filters)
            # Exact terminal aggregates are canonical for totals. Invocation
            # rows remain available for granular drill-down without mixing
            # parent and child scopes in one aggregate.
            chosen_usage = _chosen_usage()
            groups: list[dict[str, object]] = []
            if group_by:
                dimensions = [
                    (name, group_columns[name], f"group_{index}")
                    for index, name in enumerate(group_by)
                ]
                chosen_dimensions = ",".join(
                    f"{expression} AS {alias}"
                    for _name, expression, alias in dimensions
                )
                aliases = ",".join(alias for _name, _expression, alias in dimensions)
                public_dimensions = ",".join(
                    f"usage_groups.{alias} AS {name}"
                    for name, _expression, alias in dimensions
                )
                total_dimensions = ",".join(
                    f"NULL AS {name}" for name, _expression, _alias in dimensions
                )
                join_dimensions = " AND ".join(
                    f"usage_groups.{alias} IS invocation_groups.{alias}"
                    for _name, _expression, alias in dimensions
                )
                token_select = ",".join(
                    f"usage_groups.{column}" for column in TOKEN_COLUMNS
                )
                selected = conn.execute(
                    "WITH chosen AS MATERIALIZED (SELECT b.*,t.session_id,t.prompt_template_id,t.started_at_us,"
                    f"{chosen_dimensions} "
                    "FROM best_usage b JOIN turns t ON t.turn_id=b.turn_id JOIN sessions s ON s.session_id=t.session_id "
                    f"WHERE {where} AND ({chosen_usage})),"
                    "represented AS MATERIALIZED (SELECT DISTINCT " + aliases + ",invocation_id "
                    "FROM chosen WHERE scope='invocation' UNION SELECT DISTINCT " + aliases +
                    ",i.invocation_id FROM chosen fallback JOIN llm_invocations i "
                    "ON fallback.scope='turn' AND i.turn_id=fallback.turn_id),"
                    "usage_groups AS (SELECT " + aliases + "," +
                    ",".join(_strict_sum(column) for column in TOKEN_COLUMNS) +
                    ",COUNT(DISTINCT turn_id) AS turns,"
                    "COUNT(DISTINCT CASE WHEN scope='invocation' THEN turn_id END) "
                    "AS invocation_granularity_turns,"
                    "COUNT(DISTINCT CASE WHEN scope='turn' THEN turn_id END) "
                    "AS turn_granularity_turns FROM chosen GROUP BY " + aliases + "),"
                    "invocation_groups AS (SELECT " + aliases +
                    ",COUNT(*) AS invocations FROM represented GROUP BY " + aliases + ") "
                    f"SELECT 0 AS row_kind,{total_dimensions}," +
                    ",".join(_strict_sum(column) for column in TOKEN_COLUMNS) +
                    ",COUNT(DISTINCT turn_id) AS turns,"
                    "(SELECT COUNT(DISTINCT invocation_id) FROM represented) AS invocations,"
                    "COUNT(DISTINCT CASE WHEN scope='invocation' THEN turn_id END) "
                    "AS invocation_granularity_turns,"
                    "COUNT(DISTINCT CASE WHEN scope='turn' THEN turn_id END) "
                    "AS turn_granularity_turns "
                    "FROM chosen UNION ALL SELECT * FROM ("
                    f"SELECT 1 AS row_kind,{public_dimensions},{token_select},usage_groups.turns,"
                    "COALESCE(invocation_groups.invocations,0) AS invocations,"
                    "usage_groups.invocation_granularity_turns,"
                    "usage_groups.turn_granularity_turns "
                    "FROM usage_groups LEFT JOIN invocation_groups ON " + join_dimensions +
                    " ORDER BY " + ",".join(f"usage_groups.{alias}" for _name, _expression, alias in dimensions) +
                    f" LIMIT {MAX_ROWS + 1})",
                    tuple(params),
                ).fetchall()
                usage = next(row for row in selected if row["row_kind"] == 0)
                grouped = [row for row in selected if row["row_kind"] == 1]
                if len(grouped) > MAX_ROWS:
                    raise MetricsQueryError("summary grouping exceeds 200 rows; narrow filters", "query_too_large")
                groups = []
                for row in grouped:
                    group = _row(row, omit=("row_kind",)) or {}
                    group.pop("invocation_granularity_turns")
                    group.pop("turn_granularity_turns")
                    groups.append(group)
            else:
                usage = conn.execute(
                    "WITH chosen AS (SELECT b.* FROM best_usage b JOIN turns t ON t.turn_id=b.turn_id JOIN sessions s ON s.session_id=t.session_id "
                    f"WHERE {where} AND ({chosen_usage})) "
                    "SELECT " + ",".join(_strict_sum(column) for column in TOKEN_COLUMNS) +
                    ",COUNT(DISTINCT turn_id) AS turns,"
                    "(SELECT COUNT(*) FROM ("
                    "SELECT invocation_id FROM chosen WHERE scope='invocation' "
                    "UNION SELECT i.invocation_id FROM chosen fallback "
                    "JOIN llm_invocations i ON fallback.scope='turn' "
                    "AND i.turn_id=fallback.turn_id)) "
                    "AS invocations,"
                    "COUNT(DISTINCT CASE WHEN scope='invocation' THEN turn_id END) "
                    "AS invocation_granularity_turns,"
                    "COUNT(DISTINCT CASE WHEN scope='turn' THEN turn_id END) "
                    "AS turn_granularity_turns FROM chosen",
                    tuple(params),
                ).fetchone()
            totals = {column: usage[column] for column in TOKEN_COLUMNS}
            totals.update({"turns": int(usage["turns"] or 0), "invocations": int(usage["invocations"] or 0)})
            non_usage_filters = list(filters)
            tool_params = list(params)
            non_usage_filters = [
                item.replace(
                    "b.model=?",
                    "EXISTS (SELECT 1 FROM llm_invocations model_i "
                    "WHERE model_i.invocation_id=x.triggering_invocation_id "
                    "AND model_i.model_selected=?)",
                )
                for item in non_usage_filters
            ]
            non_usage_where = " AND ".join(non_usage_filters)
            totals["tool_executions"] = int(conn.execute(
                "SELECT COUNT(*) FROM tool_executions x JOIN turns t ON t.turn_id=x.turn_id JOIN sessions s ON s.session_id=t.session_id WHERE " + non_usage_where,
                tuple(tool_params),
            ).fetchone()[0])
            cost_filters = [item.replace("b.model", "c.model") for item in filters]
            costs = conn.execute(
                "WITH chosen_cost AS (SELECT c.* FROM cost_measurements c "
                "JOIN turns t ON t.turn_id=c.turn_id JOIN sessions s ON s.session_id=t.session_id WHERE " +
                " AND ".join(cost_filters) + f" AND {_chosen_cost()}) "
                "SELECT SUM(CASE WHEN is_estimated=0 THEN amount_usd END),"
                "SUM(CASE WHEN is_estimated=1 THEN amount_usd END) FROM chosen_cost",
                tuple(params),
            ).fetchone()
            totals["reported_cost_usd"], totals["estimated_cost_usd"] = costs[0], costs[1]
            coverage_filters = ["i.started_at_us>=?", "i.started_at_us<?"]
            coverage_values: list[object] = [start, end]
            if session_id:
                coverage_filters.append("i.turn_id IN (SELECT turn_id FROM turns WHERE session_id=?)")
                coverage_values.append(session_id)
            if model:
                coverage_filters.append("i.model_selected=?")
                coverage_values.append(model)
            if run_key:
                coverage_filters.append("i.turn_id IN (SELECT t.turn_id FROM turns t JOIN sessions s ON s.session_id=t.session_id WHERE s.run_key=?)")
                coverage_values.append(run_key)
            if experiment_id:
                coverage_filters.append("EXISTS (SELECT 1 FROM router_decisions r WHERE r.turn_id=i.turn_id AND r.experiment_id=?)")
                coverage_values.append(experiment_id)
            if variant_id:
                coverage_filters.append("EXISTS (SELECT 1 FROM router_decisions r WHERE r.turn_id=i.turn_id AND r.variant_id=?)")
                coverage_values.append(variant_id)
            coverage = _with_usage_granularity(
                _coverage(conn, " AND ".join(coverage_filters), tuple(coverage_values)),
                usage["invocation_granularity_turns"],
                usage["turn_granularity_turns"],
            )
            return {
                "metrics_schema_version": SCHEMA_VERSION,
                "totals": totals,
                "groups": groups,
                "coverage": coverage,
            }

        return self._run(build)

    def session(self, args: dict[str, Any]) -> dict[str, object]:
        session_id = _required_id(args, "session_id")
        include_turns = args.get("include_turns", True)
        if not isinstance(include_turns, bool):
            raise MetricsQueryError("include_turns must be a boolean", "bad_request")
        limit = _limit(args)
        filters = {"session_id": session_id, "include_turns": include_turns, "limit": limit}
        position = _decode_cursor(
            args.get("cursor"), filters, ("started_at_us", "turn_id"),
        )
        if position is not None and (
            isinstance(position["started_at_us"], bool)
            or not isinstance(position["started_at_us"], int)
            or not isinstance(position["turn_id"], str)
        ):
            raise MetricsQueryError("cursor does not match this query", "invalid_cursor")

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            session = conn.execute(
                f"SELECT {_columns(SESSION_PUBLIC_COLUMNS)} FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise MetricsQueryError(f"unknown session {session_id}", "unknown_session", session_id=session_id)
            turns: list[dict[str, object]] = []
            more = False
            if include_turns:
                after_started = position["started_at_us"] if position else None
                after_id = position["turn_id"] if position else None
                rows = conn.execute(
                    f"SELECT {_columns(TURN_PUBLIC_COLUMNS)} FROM turns "
                    "WHERE session_id=? AND (? IS NULL OR started_at_us>? "
                    "OR (started_at_us=? AND turn_id>?)) "
                    "ORDER BY started_at_us,turn_id LIMIT ?",
                    (session_id, after_started, after_started, after_started, after_id, limit + 1),
                ).fetchall()
                more = len(rows) > limit
                turns = [_row(row) or {} for row in rows[:limit]]
            next_cursor = None
            if more:
                last = turns[-1]
                next_cursor = _encode_cursor(
                    {"started_at_us": last["started_at_us"], "turn_id": last["turn_id"]},
                    filters,
                )
            return {
                "session": _row(session),
                "turns": turns,
                "next_cursor": next_cursor,
                "coverage": _coverage(conn, "i.turn_id IN (SELECT turn_id FROM turns WHERE session_id=?)", (session_id,)),
            }

        return self._run(build)

    def turn(self, args: dict[str, Any]) -> dict[str, object]:
        turn_id = _required_id(args, "turn_id")

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            turn = conn.execute(
                f"SELECT {_columns(TURN_PUBLIC_COLUMNS)} FROM turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise MetricsQueryError(f"unknown turn {turn_id}", "unknown_turn", turn_id=turn_id)
            rows = lambda sql: [_row(row) or {} for row in conn.execute(sql, (turn_id,)).fetchall()]
            steps = rows(
                f"SELECT {_columns(WORKFLOW_STEP_PUBLIC_COLUMNS)} FROM workflow_steps "
                "WHERE turn_id=? ORDER BY step_index,attempt"
            )
            invocations = rows(
                f"SELECT {_columns(INVOCATION_PUBLIC_COLUMNS)} FROM llm_invocations "
                "WHERE turn_id=? ORDER BY invocation_index"
            )
            tools = rows("SELECT tool_execution_id,turn_id,triggering_invocation_id,consuming_invocation_id,provider_tool_call_id,tool_name,tool_kind,input_bytes,output_bytes,output_estimated_tokens,started_at_us,ended_at_us,status,is_error,attribution_method,attribution_confidence FROM tool_executions WHERE turn_id=? ORDER BY started_at_us,tool_execution_id")
            usage = rows("SELECT measurement_id,scope,turn_id,invocation_id,provider,model,provider_event_id,measurement_source,is_estimated,estimator_name,estimator_version,token_semantics_version,input_tokens,uncached_input_tokens,cache_read_input_tokens,cache_write_input_tokens,cache_write_5m_input_tokens,cache_write_1h_input_tokens,cache_write_unknown_ttl_input_tokens,cache_write_breakdown_complete,output_tokens,reasoning_output_tokens,observed_at_us,supersedes_measurement_id FROM usage_measurements WHERE turn_id=? ORDER BY observed_at_us,measurement_id")
            costs = rows("SELECT cost_measurement_id,scope,session_id,turn_id,invocation_id,provider,model,amount_usd,measurement_source,is_estimated,price_snapshot_id,provider_event_id,observed_at_us FROM cost_measurements WHERE turn_id=? ORDER BY observed_at_us,cost_measurement_id")
            decisions = rows("SELECT router_decision_id,turn_id,experiment_id,variant_id,assignment_unit,assignment_status,assignment_algorithm,cohort,router_name,router_version,policy_version,feature_schema_version,candidate_models_json,model_selected,control_model,router_score,router_latency_ms,fallback_reason,decided_at_us FROM router_decisions WHERE turn_id=? ORDER BY decided_at_us")
            _enforce_row_count(steps, invocations, tools, usage, costs, decisions)
            return {
                "turn": _row(turn),
                "workflow_steps": steps,
                "invocations": invocations,
                "tool_executions": tools,
                "usage_measurements": usage,
                "cost_measurements": costs,
                "router_decisions": decisions,
                "coverage": _coverage(conn, "i.turn_id=?", (turn_id,)),
            }

        return self._run(build)

    def hotspots(self, args: dict[str, Any]) -> dict[str, object]:
        start, end = _time_range(args)
        scope = str(args.get("scope") or "invocation")
        metric = str(args.get("metric") or "input_tokens")
        limit = _limit(args, "top_n", 20)
        session_id = str(args.get("session") or args.get("session_id") or "").strip() or None
        cursor_filters = {"start": start, "end": end, "scope": scope, "metric": metric, "session": session_id, "limit": limit}
        position = _decode_cursor(
            args.get("cursor"), cursor_filters, ("value", "id", "rank"),
        )
        if position is not None and (
            isinstance(position["value"], bool)
            or not isinstance(position["value"], (int, float))
            or not math.isfinite(float(position["value"]))
            or not isinstance(position["id"], str)
            or isinstance(position["rank"], bool)
            or not isinstance(position["rank"], int)
            or position["rank"] < 1
        ):
            raise MetricsQueryError("cursor does not match this query", "invalid_cursor")
        allowed_scopes = {"session", "turn", "invocation", "tool", "prompt_template"}
        allowed_metrics = {"reported_cost", "estimated_cost", "input_tokens", "output_tokens", "latency", "tool_result_bytes"}
        if scope not in allowed_scopes or metric not in allowed_metrics:
            raise MetricsQueryError("unsupported hotspot scope or metric", "bad_request")
        if scope == "tool" and metric not in {"latency", "tool_result_bytes"}:
            raise MetricsQueryError("tool hotspots support latency or tool_result_bytes", "bad_request")
        if scope != "tool" and metric == "tool_result_bytes":
            raise MetricsQueryError("tool_result_bytes requires tool scope", "bad_request")

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            def fetch(sql: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
                after_value = position["value"] if position else None
                after_id = position["id"] if position else None
                return conn.execute(
                    "SELECT * FROM (" + sql + ") ranked "
                    "WHERE (? IS NULL OR value<? OR (value=? AND id>?)) "
                    "ORDER BY value DESC,id LIMIT ?",
                    params + (after_value, after_value, after_value, after_id, limit + 1),
                ).fetchall()

            if scope == "tool":
                value = "(x.ended_at_us-x.started_at_us)/1000.0" if metric == "latency" else "x.output_bytes"
                known = "x.ended_at_us IS NOT NULL" if metric == "latency" else "x.output_bytes IS NOT NULL"
                rows = fetch(
                    f"SELECT x.tool_execution_id AS id,x.tool_name AS label,{value} AS value,x.attribution_method,x.attribution_confidence "
                    "FROM tool_executions x JOIN turns t ON t.turn_id=x.turn_id WHERE t.started_at_us>=? AND t.started_at_us<? "
                    f"AND (? IS NULL OR t.session_id=?) AND {known}",
                    (start, end, session_id, session_id),
                )
            elif metric in {"input_tokens", "output_tokens"}:
                if scope == "invocation":
                    rows = fetch(
                        f"SELECT b.invocation_id AS id,"
                        "CASE WHEN COUNT(DISTINCT b.model)=1 THEN MIN(b.model) ELSE 'mixed' END AS label,"
                        f"SUM(b.{metric}) AS value,"
                        "CASE WHEN COUNT(DISTINCT b.measurement_source)=1 THEN MIN(b.measurement_source) ELSE 'mixed' END AS measurement_source,"
                        "MAX(b.is_estimated) AS is_estimated FROM best_usage b "
                        "JOIN turns t ON t.turn_id=b.turn_id WHERE t.started_at_us>=? AND t.started_at_us<? "
                        f"AND (? IS NULL OR t.session_id=?) AND b.scope='invocation' "
                        f"AND {_effective_usage('b')} AND b.{metric} IS NOT NULL "
                        "GROUP BY b.invocation_id HAVING COUNT(b." + metric + ")=COUNT(*)",
                        (start, end, session_id, session_id),
                    )
                else:
                    grouping = {
                        "session": ("t.session_id", "s.session_name"),
                        "turn": ("t.turn_id", "t.turn_id"),
                        "prompt_template": ("t.prompt_template_id", "t.prompt_template_id"),
                    }[scope]
                    extra = "AND t.prompt_template_id IS NOT NULL " if scope == "prompt_template" else ""
                    rows = fetch(
                        "WITH chosen AS (SELECT b.*,t.session_id,t.prompt_template_id,s.session_name "
                        "FROM best_usage b JOIN turns t ON t.turn_id=b.turn_id "
                        "JOIN sessions s ON s.session_id=t.session_id WHERE t.started_at_us>=? AND t.started_at_us<? "
                        f"AND (? IS NULL OR t.session_id=?) AND {_chosen_usage()}) "
                        f"SELECT {grouping[0]} AS id,{grouping[1]} AS label,SUM(b.{metric}) AS value,"
                        "CASE WHEN COUNT(DISTINCT b.measurement_source)=1 THEN MIN(b.measurement_source) ELSE 'mixed' END AS measurement_source,"
                        "MAX(b.is_estimated) AS is_estimated FROM chosen b "
                        "JOIN turns t ON t.turn_id=b.turn_id JOIN sessions s ON s.session_id=t.session_id "
                        f"WHERE 1=1 {extra}GROUP BY {grouping[0]},{grouping[1]} "
                        f"HAVING COUNT(b.{metric})=COUNT(*)",
                        (start, end, session_id, session_id),
                    )
            elif metric == "latency":
                if scope == "session":
                    rows = fetch(
                        "SELECT s.session_id AS id,s.session_name AS label,"
                        "(s.ended_at_us-s.started_at_us)/1000.0 AS value FROM sessions s "
                        "WHERE s.started_at_us>=? AND s.started_at_us<? AND (? IS NULL OR s.session_id=?) "
                        "AND s.ended_at_us IS NOT NULL",
                        (start, end, session_id, session_id),
                    )
                elif scope == "turn":
                    rows = fetch(
                        "SELECT t.turn_id AS id,t.turn_id AS label,t.wall_duration_ms AS value FROM turns t "
                        "WHERE t.started_at_us>=? AND t.started_at_us<? AND (? IS NULL OR t.session_id=?) "
                        "AND t.wall_duration_ms IS NOT NULL",
                        (start, end, session_id, session_id),
                    )
                elif scope == "invocation":
                    rows = fetch(
                        "SELECT i.invocation_id AS id,i.model_selected AS label,i.wall_duration_ms AS value "
                        "FROM llm_invocations i JOIN turns t ON t.turn_id=i.turn_id "
                        "WHERE i.started_at_us>=? AND i.started_at_us<? AND (? IS NULL OR t.session_id=?) "
                        "AND i.wall_duration_ms IS NOT NULL",
                        (start, end, session_id, session_id),
                    )
                else:
                    rows = fetch(
                        "SELECT t.prompt_template_id AS id,t.prompt_template_id AS label,SUM(t.wall_duration_ms) AS value "
                        "FROM turns t WHERE t.started_at_us>=? AND t.started_at_us<? "
                        "AND (? IS NULL OR t.session_id=?) AND t.prompt_template_id IS NOT NULL "
                        "GROUP BY t.prompt_template_id HAVING COUNT(t.wall_duration_ms)=COUNT(*)",
                        (start, end, session_id, session_id),
                    )
            else:
                estimated = 1 if metric == "estimated_cost" else 0
                if scope == "invocation":
                    rows = fetch(
                        "SELECT c.invocation_id AS id,"
                        "CASE WHEN COUNT(DISTINCT COALESCE(c.model,c.provider))=1 "
                        "THEN MIN(COALESCE(c.model,c.provider)) ELSE 'mixed' END AS label,"
                        "SUM(c.amount_usd) AS value,"
                        "CASE WHEN COUNT(DISTINCT c.measurement_source)=1 "
                        "THEN MIN(c.measurement_source) ELSE 'mixed' END AS measurement_source,"
                        "MAX(c.is_estimated) AS is_estimated FROM cost_measurements c "
                        "JOIN turns t ON t.turn_id=c.turn_id WHERE t.started_at_us>=? AND t.started_at_us<? "
                        "AND (? IS NULL OR t.session_id=?) AND c.scope='invocation' AND c.is_estimated=? "
                        f"AND {_effective_cost('c')} "
                        "GROUP BY c.invocation_id",
                        (start, end, session_id, session_id, estimated),
                    )
                else:
                    grouping = {
                        "session": ("t.session_id", "s.session_name"),
                        "turn": ("t.turn_id", "t.turn_id"),
                        "prompt_template": ("t.prompt_template_id", "t.prompt_template_id"),
                    }[scope]
                    extra = "AND t.prompt_template_id IS NOT NULL " if scope == "prompt_template" else ""
                    rows = fetch(
                        "WITH chosen AS (SELECT c.* FROM cost_measurements c "
                        "JOIN turns t ON t.turn_id=c.turn_id JOIN sessions s ON s.session_id=t.session_id "
                        "WHERE t.started_at_us>=? AND t.started_at_us<? AND (? IS NULL OR t.session_id=?) "
                        f"AND c.is_estimated=? AND {_chosen_cost()}) "
                        f"SELECT {grouping[0]} AS id,{grouping[1]} AS label,SUM(c.amount_usd) AS value,"
                        "CASE WHEN COUNT(DISTINCT c.measurement_source)=1 THEN MIN(c.measurement_source) ELSE 'mixed' END AS measurement_source,"
                        "MAX(c.is_estimated) AS is_estimated FROM chosen c "
                        "JOIN turns t ON t.turn_id=c.turn_id JOIN sessions s ON s.session_id=t.session_id "
                        f"WHERE 1=1 {extra}GROUP BY {grouping[0]},{grouping[1]}",
                        (start, end, session_id, session_id, estimated),
                    )
            more = len(rows) > limit
            prior_rank = int(position["rank"]) if position else 0
            hotspots = [{"rank": prior_rank + index, "scope": scope, **(_row(row) or {})} for index, row in enumerate(rows[:limit], 1)]
            next_cursor = None
            if more:
                last = hotspots[-1]
                next_cursor = _encode_cursor(
                    {"value": last["value"], "id": last["id"], "rank": last["rank"]},
                    cursor_filters,
                )
            return {
                "hotspots": hotspots,
                "next_cursor": next_cursor,
                "coverage": _coverage(conn, "i.turn_id IN (SELECT turn_id FROM turns WHERE started_at_us>=? AND started_at_us<? AND (? IS NULL OR session_id=?))", (start, end, session_id, session_id)),
            }

        return self._run(build)

    def experiment(self, args: dict[str, Any]) -> dict[str, object]:
        experiment_id = _required_id(args, "experiment_id")
        decision_filters = ["r.experiment_id=?"]
        decision_params: list[object] = [experiment_id]
        if args.get("from") is not None or args.get("to") is not None:
            start, end = _time_range(args)
            decision_filters.extend(["r.decided_at_us>=?", "r.decided_at_us<?"])
            decision_params.extend([start, end])
        cohort = str(args.get("cohort") or "").strip() or None
        if cohort:
            decision_filters.append("r.cohort=?")
            decision_params.append(cohort)
        outcome_filters: list[str] = []
        outcome_params: list[object] = []
        for argument, column in (
            ("outcome", "outcome_name"),
            ("outcome_definition_version", "outcome_definition_version"),
            ("evaluator_name", "evaluator_name"),
            ("evaluator_version", "evaluator_version"),
        ):
            value = str(args.get(argument) or "").strip() or None
            if value:
                outcome_filters.append(f"o.{column}=?")
                outcome_params.append(value)
        decision_where = " AND ".join(decision_filters)

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            exists = conn.execute(
                "SELECT 1 FROM router_decisions WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if exists is None:
                raise MetricsQueryError(f"unknown experiment {experiment_id}", "unknown_experiment", experiment_id=experiment_id)
            decisions_cte = (
                "WITH decisions AS MATERIALIZED ("
                "SELECT r.* FROM router_decisions r "
                f"WHERE {decision_where}),"
                "variant_turns AS MATERIALIZED ("
                "SELECT DISTINCT variant_id,turn_id FROM decisions) "
            )
            rows = conn.execute(
                decisions_cte
                + "SELECT d.variant_id,COUNT(*) AS sample_size,"
                "AVG(t.wall_duration_ms) AS turn_latency_ms,"
                "AVG(CASE WHEN t.status='completed' THEN 1.0 ELSE 0.0 END) AS completion_rate,"
                "AVG(CASE WHEN t.error_kind IS NOT NULL OR t.status IN ('failed','error','crashed') "
                "THEN 1.0 ELSE 0.0 END) AS error_rate,"
                "AVG(CASE WHEN d.fallback_reason IS NOT NULL THEN 1.0 ELSE 0.0 END) AS fallback_rate "
                "FROM decisions d JOIN turns t ON t.turn_id=d.turn_id "
                "GROUP BY d.variant_id ORDER BY d.variant_id",
                tuple(decision_params),
            ).fetchall()
            assignment_rows = conn.execute(
                decisions_cte
                + "SELECT variant_id,COUNT(*) AS assignment_sample_size,"
                "AVG(router_latency_ms) AS router_latency_ms FROM ("
                "SELECT d.variant_id,d.router_latency_ms,"
                "ROW_NUMBER() OVER (PARTITION BY d.variant_id,"
                "CASE WHEN d.assignment_status='assigned' "
                "AND d.assignment_unit IS NOT NULL "
                "AND d.assignment_key_hash IS NOT NULL "
                "THEN d.assignment_unit || ':' || d.assignment_key_hash "
                "ELSE 'turn:' || d.turn_id END "
                "ORDER BY d.decided_at_us,d.turn_id) AS assignment_rank "
                "FROM decisions d) WHERE assignment_rank=1 GROUP BY variant_id",
                tuple(decision_params),
            ).fetchall()
            arm_units = conn.execute(
                decisions_cte
                + "SELECT variant_id,COUNT(*) AS assignment_unit_count,"
                "AVG(turn_count) AS turns_per_unit,AVG(completed) AS completion_rate,"
                "AVG(errored) AS error_rate,AVG(latency_ms) AS latency_ms_per_unit,"
                "AVG(retry_steps) AS observed_workflow_retry_steps_per_unit,"
                "AVG(reported_cost_usd) AS known_reported_cost_usd_per_unit,"
                "SUM(CASE WHEN reported_cost_usd IS NULL THEN 1 ELSE 0 END) AS units_without_reported_cost FROM ("
                "SELECT d.variant_id,d.assignment_unit,d.assignment_key_hash,"
                "COUNT(DISTINCT d.turn_id) AS turn_count,"
                "MIN(CASE WHEN t.status='completed' THEN 1.0 ELSE 0.0 END) AS completed,"
                "MAX(CASE WHEN t.error_kind IS NOT NULL OR t.status IN ('failed','error','crashed') "
                "THEN 1.0 ELSE 0.0 END) AS errored,SUM(t.wall_duration_ms) AS latency_ms,"
                "SUM((SELECT COUNT(*) FROM workflow_steps ws WHERE ws.turn_id=d.turn_id "
                "AND ws.attempt>1)) AS retry_steps,"
                "CASE WHEN COUNT((SELECT SUM(c.amount_usd) FROM cost_measurements c "
                "WHERE c.turn_id=d.turn_id AND c.is_estimated=0 AND " + _chosen_cost("c") + "))=COUNT(*) "
                "THEN SUM((SELECT SUM(c.amount_usd) FROM cost_measurements c "
                "WHERE c.turn_id=d.turn_id AND c.is_estimated=0 AND " + _chosen_cost("c") + ")) END AS reported_cost_usd "
                "FROM decisions d JOIN turns t ON t.turn_id=d.turn_id "
                "WHERE d.assignment_status='assigned' AND d.assignment_unit IS NOT NULL "
                "AND d.assignment_key_hash IS NOT NULL "
                "GROUP BY d.variant_id,d.assignment_unit,d.assignment_key_hash) "
                "GROUP BY variant_id ORDER BY variant_id",
                tuple(decision_params),
            ).fetchall()
            decision_metadata = conn.execute(
                decisions_cte
                + "SELECT variant_id,assignment_status,metadata_json FROM decisions",
                tuple(decision_params),
            ).fetchall()
            usage_rows = conn.execute(
                decisions_cte
                + "SELECT vt.variant_id,"
                + ",".join(
                    f"CASE WHEN COUNT(b.{column})=COUNT(*) "
                    f"THEN SUM(b.{column}) END AS {column}"
                    for column in TOKEN_COLUMNS
                )
                + ",COUNT(DISTINCT b.turn_id) AS measured_turns,"
                "COUNT(DISTINCT CASE WHEN b.is_estimated=0 THEN b.turn_id END) "
                "AS exact_measurement_turns,"
                "COUNT(DISTINCT CASE WHEN b.is_estimated=1 THEN b.turn_id END) "
                "AS estimated_measurement_turns,"
                "COUNT(DISTINCT CASE WHEN b.scope='invocation' THEN b.turn_id END) "
                "AS invocation_granularity_turns,"
                "COUNT(DISTINCT CASE WHEN b.scope='turn' THEN b.turn_id END) "
                "AS turn_granularity_turns "
                "FROM variant_turns vt JOIN best_usage b ON b.turn_id=vt.turn_id "
                f"WHERE {_chosen_usage('b')} GROUP BY vt.variant_id",
                tuple(decision_params),
            ).fetchall()
            cost_rows = conn.execute(
                decisions_cte
                + "SELECT vt.variant_id,"
                "SUM(CASE WHEN c.is_estimated=0 THEN c.amount_usd END) AS reported_cost_usd,"
                "SUM(CASE WHEN c.is_estimated=1 THEN c.amount_usd END) AS estimated_cost_usd "
                "FROM variant_turns vt JOIN cost_measurements c ON c.turn_id=vt.turn_id "
                f"WHERE {_chosen_cost('c')} GROUP BY vt.variant_id",
                tuple(decision_params),
            ).fetchall()
            coverage_rows = conn.execute(
                decisions_cte
                + "SELECT vt.variant_id,COUNT(i.invocation_id) AS total_invocations,"
                "COALESCE(SUM(EXISTS (SELECT 1 FROM usage_measurements u "
                "WHERE u.invocation_id=i.invocation_id AND u.scope='invocation' "
                "AND u.is_estimated=0 AND NOT EXISTS (SELECT 1 FROM usage_measurements newer "
                "WHERE newer.supersedes_measurement_id=u.measurement_id))),0) AS exact_invocations,"
                "COALESCE(SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM usage_measurements u "
                "WHERE u.invocation_id=i.invocation_id AND u.scope='invocation' "
                "AND u.is_estimated=0 AND NOT EXISTS (SELECT 1 FROM usage_measurements newer "
                "WHERE newer.supersedes_measurement_id=u.measurement_id)) AND EXISTS ("
                "SELECT 1 FROM usage_measurements u WHERE u.invocation_id=i.invocation_id "
                "AND u.scope='invocation' AND u.is_estimated=1 AND NOT EXISTS ("
                "SELECT 1 FROM usage_measurements newer "
                "WHERE newer.supersedes_measurement_id=u.measurement_id)) THEN 1 ELSE 0 END),0) "
                "AS estimated_invocations "
                "FROM variant_turns vt LEFT JOIN llm_invocations i ON i.turn_id=vt.turn_id "
                "GROUP BY vt.variant_id",
                tuple(decision_params),
            ).fetchall()
            outcome_where = decision_where
            outcome_values = list(decision_params)
            outcomes = conn.execute(
                "SELECT r.variant_id,o.outcome_name,o.outcome_definition_version,"
                "o.outcome_source,o.evaluator_name,o.evaluator_version,o.is_estimated,"
                "COUNT(*) AS sample_size,AVG(o.outcome_value) AS mean_value "
                "FROM router_decisions r JOIN experiment_outcomes o "
                f"ON o.router_decision_id=r.router_decision_id WHERE {outcome_where} "
                + (" AND " + " AND ".join(outcome_filters) if outcome_filters else "") +
                " GROUP BY r.variant_id,o.outcome_name,o.outcome_definition_version,"
                "o.outcome_source,o.evaluator_name,o.evaluator_version,o.is_estimated "
                "ORDER BY r.variant_id,o.outcome_name,o.outcome_definition_version,"
                "o.evaluator_name,o.evaluator_version,o.is_estimated",
                tuple(outcome_values + (outcome_params if outcome_filters else [])),
            ).fetchall()
            unit_quality = conn.execute(
                "SELECT variant_id,outcome_name,outcome_definition_version,outcome_source,"
                "evaluator_name,evaluator_version,is_estimated,COUNT(*) AS assignment_unit_count,"
                "AVG(unit_mean) AS mean_unit_value FROM ("
                "SELECT r.variant_id,r.assignment_unit,r.assignment_key_hash,"
                "o.outcome_name,o.outcome_definition_version,o.outcome_source,"
                "o.evaluator_name,o.evaluator_version,o.is_estimated,AVG(o.outcome_value) AS unit_mean "
                "FROM router_decisions r JOIN experiment_outcomes o ON o.router_decision_id=r.router_decision_id "
                f"WHERE {outcome_where} AND r.assignment_status='assigned' "
                "AND r.assignment_unit IS NOT NULL AND r.assignment_key_hash IS NOT NULL "
                "AND o.outcome_source IN ('evaluator','human') "
                + (" AND " + " AND ".join(outcome_filters) if outcome_filters else "") +
                " GROUP BY r.variant_id,r.assignment_unit,r.assignment_key_hash,o.outcome_name,"
                "o.outcome_definition_version,o.outcome_source,o.evaluator_name,o.evaluator_version,o.is_estimated) "
                "GROUP BY variant_id,outcome_name,outcome_definition_version,outcome_source,"
                "evaluator_name,evaluator_version,is_estimated",
                tuple(outcome_values + (outcome_params if outcome_filters else [])),
            ).fetchall()
            usage_by_variant = {
                row["variant_id"]: _row(row, omit=("variant_id",)) or {}
                for row in usage_rows
            }
            costs_by_variant = {
                row["variant_id"]: _row(row, omit=("variant_id",)) or {}
                for row in cost_rows
            }
            coverage_by_variant = {}
            for row in coverage_rows:
                total = int(row["total_invocations"] or 0)
                exact = int(row["exact_invocations"] or 0)
                estimated = int(row["estimated_invocations"] or 0)
                coverage_by_variant[row["variant_id"]] = {
                    "exact_invocations": exact,
                    "estimated_invocations": estimated,
                    "unknown_invocations": max(0, total - exact - estimated),
                }
            assignments_by_variant = {
                row["variant_id"]: _row(row, omit=("variant_id",)) or {}
                for row in assignment_rows
            }
            variants = []
            for row in rows:
                variant = _row(row) or {}
                variant_id = row["variant_id"]
                assignment = assignments_by_variant.get(variant_id, {})
                variant["assignment_sample_size"] = int(
                    assignment.get("assignment_sample_size") or 0
                )
                variant["router_latency_ms"] = assignment.get("router_latency_ms")
                usage = usage_by_variant.get(variant_id, {})
                variant["tokens"] = {
                    column: usage.get(column) for column in TOKEN_COLUMNS
                }
                variant["reported_cost_usd"] = costs_by_variant.get(
                    variant_id, {},
                ).get("reported_cost_usd")
                variant["estimated_cost_usd"] = costs_by_variant.get(
                    variant_id, {},
                ).get("estimated_cost_usd")
                variant["coverage"] = coverage_by_variant.get(variant_id, {
                    "exact_invocations": 0,
                    "estimated_invocations": 0,
                    "unknown_invocations": 0,
                })
                variant["coverage"] = _with_usage_granularity(
                    variant["coverage"],
                    usage.get("invocation_granularity_turns"),
                    usage.get("turn_granularity_turns"),
                )
                exact_turns = int(usage.get("exact_measurement_turns") or 0)
                estimated_turns = int(usage.get("estimated_measurement_turns") or 0)
                sample_size = int(row["sample_size"] or 0)
                variant["coverage"].update({
                    "exact_measurement_turns": exact_turns,
                    "estimated_measurement_turns": estimated_turns,
                    "unknown_measurement_turns": max(
                        0, sample_size - exact_turns - estimated_turns
                    ),
                })
                variants.append(variant)
            variant_samples = {
                row["variant_id"]: int(row["sample_size"] or 0) for row in rows
            }
            outcome_views = [_row(row) or {} for row in outcomes]
            _enforce_row_count(variants, outcome_views)
            coverage_where = (
                "EXISTS (SELECT 1 FROM router_decisions r WHERE r.turn_id=i.turn_id AND "
                + decision_where + ")"
            )
            expected_weights: dict[str, float] = {}
            config_fingerprints: set[str] = set()
            missing_config_fingerprints = 0
            for decision in decision_metadata:
                try:
                    metadata = json.loads(decision["metadata_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                fingerprint = metadata.get("config_fingerprint")
                if isinstance(fingerprint, str) and fingerprint:
                    config_fingerprints.add(fingerprint)
                else:
                    missing_config_fingerprints += 1
                weights = metadata.get("expected_weights")
                if isinstance(weights, dict):
                    for variant_id, weight in weights.items():
                        if (
                            isinstance(variant_id, str)
                            and not isinstance(weight, bool)
                            and isinstance(weight, (int, float))
                            and float(weight) > 0
                        ):
                            expected_weights[variant_id] = float(weight)
            observed_samples = {
                variant["variant_id"]: int(variant["assignment_sample_size"] or 0)
                for variant in variants if variant["variant_id"] is not None
            }
            expected_variants = list(expected_weights)
            if len(expected_variants) >= 2:
                srm = sample_ratio_mismatch(
                    [observed_samples.get(variant_id, 0) for variant_id in expected_variants],
                    [expected_weights[variant_id] for variant_id in expected_variants],
                )
                srm["expected_weights"] = dict(expected_weights)
                srm["observed"] = {
                    variant_id: observed_samples.get(variant_id, 0)
                    for variant_id in expected_variants
                }
            else:
                srm = {
                    "sample_size": sum(observed_samples.values()),
                    "chi_square": None,
                    "degrees_of_freedom": max(0, len(observed_samples) - 1),
                    "p_value": None,
                    "alpha": 0.001,
                    "detected": False,
                    "expected_weights": None,
                    "status": "expected_weights_unavailable",
                }
            coverage_rates = {}
            for variant in variants:
                coverage = variant["coverage"]
                total = int(variant["sample_size"] or 0)
                coverage_rates[variant["variant_id"]] = {
                    "total_turns": total,
                    "exact_rate": (
                        coverage["exact_measurement_turns"] / total if total else None
                    ),
                    "estimated_rate": (
                        coverage["estimated_measurement_turns"] / total if total else None
                    ),
                }
            exact_rates = [
                row["exact_rate"] for row in coverage_rates.values()
                if row["exact_rate"] is not None
            ]
            estimated_rates = [
                row["estimated_rate"] for row in coverage_rates.values()
                if row["estimated_rate"] is not None
            ]
            outcome_identity_fields = (
                "outcome_name", "outcome_definition_version", "outcome_source",
                "evaluator_name", "evaluator_version", "is_estimated",
            )
            outcome_samples = {
                (
                    outcome["variant_id"],
                    *(outcome[field] for field in outcome_identity_fields),
                ): int(outcome["sample_size"] or 0)
                for outcome in outcome_views
            }
            outcome_identities = sorted({
                tuple(outcome[field] for field in outcome_identity_fields)
                for outcome in outcome_views
            }, key=lambda item: tuple(str(value) for value in item))
            outcome_missing = []
            for variant_id, variant_sample_size in variant_samples.items():
                for identity in outcome_identities:
                    observed = outcome_samples.get((variant_id, *identity), 0)
                    missing = max(0, variant_sample_size - observed)
                    outcome_missing.append({
                        "variant_id": variant_id,
                        **dict(zip(outcome_identity_fields, identity)),
                        "missing_count": missing,
                        "missing_rate": (
                            missing / variant_sample_size
                            if variant_sample_size else None
                        ),
                    })
            policy_groups: dict[tuple[object, ...], dict[str, object]] = {}
            policy_calls: dict[str, float | None] = {}
            arm_policy_calls: dict[object, dict[str, float | None]] = {}
            policy_truncated = False
            policy_scan_limit = 10000
            scanned = 0
            high_confidence_units: set[tuple[object, object, object]] = set()
            evaluator_failure_units = {
                (row["variant_id"], row["assignment_unit"], row["assignment_key_hash"])
                for row in conn.execute(
                    decisions_cte + "SELECT DISTINCT d.variant_id,d.assignment_unit,d.assignment_key_hash "
                    "FROM decisions d JOIN experiment_outcomes o ON o.router_decision_id=d.router_decision_id "
                    "WHERE d.assignment_status='assigned' AND d.assignment_unit IS NOT NULL "
                    "AND d.assignment_key_hash IS NOT NULL AND o.outcome_source IN ('evaluator','human') "
                    "AND o.is_estimated=0 AND CASE WHEN json_valid(o.metadata_json) "
                    "THEN json_type(o.metadata_json,'$.is_failure') END='true'"
                    + (" AND " + " AND ".join(outcome_filters) if outcome_filters else ""),
                    tuple(decision_params + outcome_params),
                )
            }
            runtime_error_units = {
                (row["variant_id"], row["assignment_unit"], row["assignment_key_hash"])
                for row in conn.execute(
                    decisions_cte + "SELECT DISTINCT d.variant_id,d.assignment_unit,d.assignment_key_hash "
                    "FROM decisions d JOIN turns t ON t.turn_id=d.turn_id "
                    "WHERE d.assignment_status='assigned' AND d.assignment_unit IS NOT NULL "
                    "AND d.assignment_key_hash IS NOT NULL AND "
                    "(t.error_kind IS NOT NULL OR t.status IN ('failed','error','crashed'))",
                    tuple(decision_params),
                )
            }
            for decision in conn.execute(
                "SELECT r.*,t.status AS turn_status,t.error_kind AS turn_error_kind "
                f"FROM router_decisions r JOIN turns t ON t.turn_id=r.turn_id WHERE {decision_where} "
                "ORDER BY r.decided_at_us,r.router_decision_id LIMIT ?",
                [*decision_params, policy_scan_limit + 1],
            ):
                scanned += 1
                if scanned > policy_scan_limit:
                    policy_truncated = True
                    break
                try:
                    metadata = json.loads(decision["metadata_json"] or "{}")
                    policy = metadata.get("policy", {})
                    call_id = policy.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        cost = policy.get("cost_usd")
                        policy_calls.setdefault(call_id, cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None)
                        if decision["assignment_status"] == "assigned":
                            arm_policy_calls.setdefault(decision["variant_id"], {}).setdefault(
                                call_id, policy_calls[call_id],
                            )
                    recommendation = policy.get("recommended_model")
                    if not recommendation:
                        continue
                    confidence = policy.get("confidence")
                    bucket = "high" if (isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool) and math.isfinite(confidence)
                        and 0.9 <= confidence <= 1) else "below_0_9"
                    if bucket == "high" and decision["assignment_status"] == "assigned":
                        high_confidence_units.add((decision["variant_id"], decision["assignment_unit"], decision["assignment_key_hash"]))
                    if not isinstance(recommendation, str) or not isinstance(policy.get("mode"), str):
                        continue
                    tier = policy.get("tier")
                    if tier is not None and not isinstance(tier, str):
                        continue
                    key = (decision["variant_id"], recommendation, bucket, tier, policy.get("mode"))
                    if key not in policy_groups and len(policy_groups) >= MAX_ROWS:
                        policy_truncated = True
                        continue
                    group = policy_groups.setdefault(key, {
                        "variant_id": key[0], "recommended_model": key[1],
                        "confidence_bucket": key[2], "tier": key[3], "mode": key[4],
                        "turn_count": 0, "assignment_units": set(),
                    })
                    group["turn_count"] += 1
                    group["assignment_units"].add((decision["assignment_unit"], decision["assignment_key_hash"]))
                except (ValueError, TypeError, AttributeError):
                    continue
            policy_breakdown = []
            for group in policy_groups.values():
                units = group.pop("assignment_units")
                group["assignment_unit_count"] = len(units)
                policy_breakdown.append(group)
            arm_views = []
            for row in arm_units:
                arm = dict(row)
                calls = arm_policy_calls.get(arm["variant_id"], {})
                known_cost = sum(cost for cost in calls.values() if cost is not None)
                unknown_calls = sum(cost is None for cost in calls.values())
                arm["known_policy_cost_usd"] = known_cost
                arm["unknown_policy_cost_calls"] = unknown_calls
                arm["cost_scan_truncated"] = policy_truncated
                arm["reported_total_cost_usd_per_unit"] = (
                    arm["known_reported_cost_usd_per_unit"]
                    + known_cost / arm["assignment_unit_count"]
                    if not policy_truncated and not unknown_calls
                    and not arm["units_without_reported_cost"] else None
                )
                arm_views.append(arm)
            quality_views = [dict(row) for row in unit_quality]
            _enforce_row_count(variants, outcome_views, arm_views, quality_views, policy_breakdown)
            quality_identities = {
                tuple(row[field] for field in outcome_identity_fields)
                for row in quality_views
            }
            quality_missingness = []
            for arm in arm_views:
                for identity in sorted(quality_identities, key=str):
                    observed = next((row["assignment_unit_count"] for row in quality_views
                        if row["variant_id"] == arm["variant_id"]
                        and tuple(row[field] for field in outcome_identity_fields) == identity), 0)
                    total = arm["assignment_unit_count"]
                    quality_missingness.append({
                        "variant_id": arm["variant_id"],
                        **dict(zip(outcome_identity_fields, identity)),
                        "assignment_unit_count": total,
                        "units_with_outcome": observed,
                        "units_without_outcome": total - observed,
                        "missing_rate": (total - observed) / total if total else None,
                    })
                    _enforce_row_count(variants, outcome_views, arm_views, quality_views,
                                       quality_missingness, policy_breakdown)
            _enforce_row_count(variants, outcome_views, arm_views, quality_views,
                               quality_missingness, policy_breakdown)
            return {
                "experiment_id": experiment_id,
                "intent_to_treat": {
                    "unit": "assignment_unit",
                    "completion_definition": "all_observed_turns_completed",
                    "error_definition": "any_observed_turn_error",
                    "arms": arm_views,
                    "quality": quality_views,
                    "quality_missingness": quality_missingness,
                },
                "policy_breakdown": {
                    "interpretation": "descriptive_only_not_causal",
                    "truncated": policy_truncated,
                    "scan_limit": policy_scan_limit,
                    "high_confidence_runtime_error_units": len(high_confidence_units & runtime_error_units),
                    "high_confidence_evaluator_failure_units": len(high_confidence_units & evaluator_failure_units),
                    "groups": policy_breakdown,
                    "unique_policy_calls": len(policy_calls),
                    "known_policy_cost_usd": sum(cost for cost in policy_calls.values() if cost is not None),
                    "unknown_policy_cost_calls": sum(cost is None for cost in policy_calls.values()),
                },
                "variants": variants,
                "outcomes": outcome_views,
                "diagnostics": {
                    "sample_ratio_mismatch": srm,
                    "outcome_missingness": outcome_missing,
                    "missing_outcomes": sum(
                        int(outcome["missing_count"]) for outcome in outcome_missing
                    ),
                    "coverage_rates": coverage_rates,
                    "exact_coverage_rate_spread": (
                        max(exact_rates) - min(exact_rates)
                        if len(exact_rates) >= 2 else None
                    ),
                    "estimated_coverage_rate_spread": (
                        max(estimated_rates) - min(estimated_rates)
                        if len(estimated_rates) >= 2 else None
                    ),
                    "config_consistent": (
                        len(config_fingerprints) == 1
                        and missing_config_fingerprints == 0
                    ),
                    "missing_config_fingerprints": missing_config_fingerprints,
                },
                "coverage": _coverage(conn, coverage_where, tuple(decision_params)),
            }

        return self._run(build)
