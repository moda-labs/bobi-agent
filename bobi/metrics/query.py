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

ROUTING_PUBLIC_SQL = (
    "d.router_decision_id,d.variant_id,d.model_selected,d.fallback_reason,"
    "d.router_latency_ms,json_extract(d.metadata_json,'$.policy.mode') AS policy_mode,"
    "json_extract(d.metadata_json,'$.policy.status') AS policy_status,"
    "json_extract(d.metadata_json,'$.policy.recommended_model') AS recommended_model,"
    "json_extract(d.metadata_json,'$.policy.confidence') AS confidence,"
    "json_extract(d.metadata_json,'$.policy.latency_ms') AS policy_latency_ms,"
    "json_extract(d.metadata_json,'$.policy.call_id') AS policy_call_id"
)


def _columns(columns: tuple[str, ...]) -> str:
    return ",".join(columns)


def _strict_sum(column: str) -> str:
    """Aggregate a provider dimension without turning partial knowledge into fact."""
    return f"CASE WHEN COUNT({column})=COUNT(*) THEN SUM({column}) END AS {column}"


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
    effective_invocations = _complete_effective_invocation_usage(alias)
    exact_turn = _exact_turn_usage_exists(alias)
    aliased_terminal_turn = _single_invocation_model_has_exact_turn(alias)
    return (
        f"({_effective_usage(alias)} AND ("
        f"({alias}.scope='invocation' AND NOT ({exact_turn}) "
        f"AND NOT ({aliased_terminal_turn}) AND "
        f"({effective_invocations})) OR "
        f"({alias}.scope='turn' AND (({exact_turn}) OR "
        f"(NOT ({effective_invocations}))))"
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
            conn.execute("BEGIN")
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
        session_name = str(args.get("session_name") or "").strip() or None
        model = str(args.get("model") or "").strip() or None
        run_key = str(args.get("run_key") or "").strip() or None
        experiment_id = str(args.get("experiment_id") or "").strip() or None
        variant_id = str(args.get("variant_id") or "").strip() or None
        routing_mode = str(args.get("routing") or "").strip().lower() or None
        dashboard = args.get("include_dashboard", False)
        if not isinstance(dashboard, bool):
            raise MetricsQueryError("include_dashboard must be a boolean", "bad_request")
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
            if session_name:
                filters.append("s.session_name=?")
                params.append(session_name)
            if model:
                if dashboard:
                    filters.append("EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id=t.turn_id AND (i.model_selected=? OR i.model_requested=?))")
                    params.extend([model, model])
                else:
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
            if routing_mode in {"jev", "routed"}:
                filters.append("EXISTS (SELECT 1 FROM router_decisions rd WHERE rd.turn_id=t.turn_id)")
            elif routing_mode in {"direct", "baseline", "none"}:
                filters.append("NOT EXISTS (SELECT 1 FROM router_decisions rd WHERE rd.turn_id=t.turn_id)")
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
            if session_name:
                coverage_filters.append("i.turn_id IN (SELECT t.turn_id FROM turns t JOIN sessions s ON s.session_id=t.session_id WHERE s.session_name=?)")
                coverage_values.append(session_name)
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
            payload = {
                "metrics_schema_version": SCHEMA_VERSION,
                "totals": totals,
                "groups": groups,
                "coverage": coverage,
            }
            if dashboard:
                bucket_us = 3_600_000_000 if end - start <= 2 * 86_400_000_000 else 86_400_000_000
                buckets = conn.execute(
                    "SELECT (t.started_at_us / ?) * ? AS started_at_us," +
                    ",".join(_strict_sum(column) for column in TOKEN_COLUMNS) +
                    ",MAX(b.is_estimated) AS is_estimated FROM best_usage b "
                    "JOIN turns t ON t.turn_id=b.turn_id JOIN sessions s ON s.session_id=t.session_id "
                    f"WHERE {where} AND ({chosen_usage}) GROUP BY 1 ORDER BY 1",
                    (bucket_us, bucket_us, *params),
                ).fetchall()
                routing_filters = [item.replace("b.model=?", "EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id=t.turn_id AND i.model_selected=?)") for item in filters]
                decision_where = " AND ".join(routing_filters)
                routing = conn.execute(
                    "SELECT COUNT(*) AS turns,COUNT(d.router_decision_id) AS routed_turns,"
                    "COUNT(DISTINCT json_extract(d.metadata_json,'$.policy.call_id')) AS policy_calls,"
                    "SUM(CASE WHEN d.fallback_reason IS NOT NULL OR "
                    "json_extract(s.metadata_json,'$.router_fallback.fallback_reason') IS NOT NULL "
                    "THEN 1 ELSE 0 END) AS fallback_turns,"
                    "AVG(d.router_latency_ms) AS mean_router_latency_ms "
                    "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                    "LEFT JOIN router_decisions d ON d.router_decision_id=(SELECT latest.router_decision_id "
                    "FROM router_decisions latest WHERE latest.turn_id=t.turn_id "
                    "ORDER BY latest.decided_at_us DESC,latest.router_decision_id DESC LIMIT 1) "
                    f"WHERE {decision_where}", tuple(params),
                ).fetchone()
                models = conn.execute(
                    "SELECT d.model_selected,COUNT(*) AS turns FROM turns t "
                    "JOIN sessions s ON s.session_id=t.session_id JOIN router_decisions d "
                    "ON d.router_decision_id=(SELECT latest.router_decision_id FROM router_decisions latest "
                    "WHERE latest.turn_id=t.turn_id ORDER BY latest.decided_at_us DESC,"
                    f"latest.router_decision_id DESC LIMIT 1) WHERE {decision_where} "
                    "GROUP BY d.model_selected ORDER BY turns DESC LIMIT ?",
                    (*params, MAX_ROWS + 1),
                ).fetchall()
                _enforce_row_count(list(buckets), list(models))
                payload.update(
                    range={"from_us": start, "to_us": end, "bucket_seconds": bucket_us // 1_000_000},
                    buckets=[_row(row) for row in buckets],
                    routing={**(_row(routing) or {}), "models": [_row(row) for row in models]},
                )
            return payload

        return self._run(build)

    def turns(self, args: dict[str, Any]) -> dict[str, object]:
        start, end = _time_range(args)
        limit = _limit(args)
        filters = {"from_us": start, "to_us": end, "limit": limit}
        conditions = ["t.started_at_us>=?", "t.started_at_us<?"]
        params: list[object] = [start, end]
        for key, column in (("session_id", "t.session_id"), ("session_name", "s.session_name"),
                            ("run_key", "s.run_key")):
            if args.get(key):
                value = _required_id(args, key)
                filters[key] = value
                conditions.append(f"{column}=?")
                params.append(value)
        if args.get("model"):
            model = _required_id(args, "model")
            filters["model"] = model
            conditions.append("EXISTS (SELECT 1 FROM llm_invocations i WHERE i.turn_id=t.turn_id AND (i.model_selected=? OR i.model_requested=?))")
            params.extend([model, model])
        if args.get("routing"):
            routing = _required_id(args, "routing").lower()
            filters["routing"] = routing
            if routing in {"jev", "routed"}:
                conditions.append("EXISTS (SELECT 1 FROM router_decisions rd WHERE rd.turn_id=t.turn_id)")
            elif routing in {"direct", "baseline", "none"}:
                conditions.append("NOT EXISTS (SELECT 1 FROM router_decisions rd WHERE rd.turn_id=t.turn_id)")
        position = _decode_cursor(args.get("cursor"), filters, ("started_at_us", "turn_id"))
        if position is not None:
            if type(position["started_at_us"]) is not int or not isinstance(position["turn_id"], str):
                raise MetricsQueryError("invalid cursor position", "invalid_cursor")
            conditions.append("(t.started_at_us,t.turn_id)<(?,?)")
            params.extend((position["started_at_us"], position["turn_id"]))

        def build(conn: sqlite3.Connection) -> dict[str, object]:
            selected = conn.execute(
                "SELECT t.turn_id,t.session_id,t.turn_index,t.started_at_us,t.status,t.wall_duration_ms,"
                "s.session_name,s.role," + ROUTING_PUBLIC_SQL + ","
                "json_extract(s.metadata_json,'$.router_fallback.fallback_reason') AS session_fallback_reason,"
                "CASE WHEN json_extract(d.metadata_json,'$.policy.call_id') IS NOT NULL THEN EXISTS ("
                "SELECT 1 FROM router_decisions prior JOIN turns previous ON previous.turn_id=prior.turn_id "
                "WHERE json_extract(prior.metadata_json,'$.policy.call_id')="
                "json_extract(d.metadata_json,'$.policy.call_id') AND "
                "(previous.started_at_us,previous.turn_id)<(t.started_at_us,t.turn_id)) ELSE NULL END AS route_reused "
                "FROM turns t JOIN sessions s ON s.session_id=t.session_id "
                "LEFT JOIN router_decisions d ON d.router_decision_id=(SELECT latest.router_decision_id "
                "FROM router_decisions latest WHERE latest.turn_id=t.turn_id "
                "ORDER BY latest.decided_at_us DESC,latest.router_decision_id DESC LIMIT 1) "
                "WHERE " + " AND ".join(conditions) + " ORDER BY t.started_at_us DESC,t.turn_id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            rows = [_row(row) or {} for row in selected[:limit]]
            for row in rows:
                usage = conn.execute(
                    "SELECT " + ",".join(_strict_sum(column) for column in TOKEN_COLUMNS) +
                    ",MAX(b.is_estimated) AS is_estimated,GROUP_CONCAT(DISTINCT b.measurement_source) AS sources "
                    f"FROM best_usage b WHERE b.turn_id=? AND ({_chosen_usage()})", (row["turn_id"],),
                ).fetchone()
                row["usage"] = _row(usage)
                row["invocations"] = [_row(invocation) for invocation in conn.execute(
                    "SELECT provider,model_requested,model_selected,status FROM llm_invocations "
                    "WHERE turn_id=? ORDER BY invocation_index LIMIT ?", (row["turn_id"], MAX_ROWS + 1),
                )]
                _enforce_row_count(row["invocations"])
            next_cursor = None
            if len(selected) > limit:
                last = rows[-1]
                next_cursor = _encode_cursor(
                    {"started_at_us": last["started_at_us"], "turn_id": last["turn_id"]}, filters,
                )
            return {"turns": rows, "next_cursor": next_cursor,
                    "range": {"from_us": start, "to_us": end}}

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
        include_policy = args.get("include_policy", False)
        if not isinstance(include_policy, bool):
            raise MetricsQueryError("include_policy must be a boolean", "bad_request")

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
            if include_policy:
                public_policy = {row["router_decision_id"]: row for row in rows(
                    "SELECT " + ROUTING_PUBLIC_SQL + " FROM router_decisions d WHERE d.turn_id=?"
                )}
                for decision in decisions:
                    decision.update(public_policy[decision["router_decision_id"]])
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
