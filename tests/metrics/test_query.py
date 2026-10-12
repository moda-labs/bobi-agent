"""Operational query and command contract regressions."""

import httpx
import json
import sqlite3
import time
import pytest
from bobi.admin_client import AdminClientError, run_admin_command
from bobi.cli import main, metrics
from bobi.metrics.query import MAX_RESPONSE_BYTES, MAX_ROWS, MetricsQueries, MetricsQueryError
from bobi.metrics.store import connect, fill_model_keys, migrate
from click.testing import CliRunner
from tests.metrics.helpers import rekey, seed_dashboard


def test_dashboard_canonical_usage_routing_and_privacy(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z"}
    summary = queries.summary({**args, "include_dashboard": True})
    assert summary["totals"]["input_tokens"] == 720
    assert summary["totals"]["output_tokens"] == 140
    assert summary["totals"]["cache_write_input_tokens"] is None
    assert summary["buckets"][0]["input_tokens"] == 720
    assert summary["buckets"][0]["is_estimated"] == 0
    assert summary["routing"]["policy_calls"] == 2
    assert summary["routing"]["routed_turns"] == 3
    assert summary["routing"]["fallback_turns"] == 1
    turns = queries.turns(args)["turns"]
    assert turns[0]["session_fallback_reason"] == "invalid_config"
    assert turns[0]["usage"]["input_tokens"] is None
    assert turns[1]["route_reused"] == 1
    assert turns[1]["usage"]["input_tokens"] == 320
    assert turns[-1]["route_reused"] == 0
    assert turns[-1]["confidence"] == 0.72
    assert turns[-1]["usage"]["output_tokens"] == 0
    assert turns[-1]["tool_count"] == 0
    # No recorded dollars; the list-price fallback rides separately.
    assert turns[-1]["costs"] == {"reported_cost_usd": None, "estimated_cost_usd": None,
                                  "list_price_usd": pytest.approx(100 * 1.0 / 1_000_000)}
    assert turns[0]["costs"]["list_price_usd"] is None
    detail = queries.turn({"turn_id": "t-pro", "include_policy": True})
    assert detail["router_decisions"][0]["confidence"] == 0.96
    assert "private-task" not in json.dumps([summary, turns, detail])
    filtered = queries.summary({**args, "include_dashboard": True, "model": "claude-sonnet-5-5"})
    assert filtered["totals"]["input_tokens"] == 620
    assert filtered["routing"]["turns"] == 2
    from bobi.costs import estimate_cost

    # Chosen usage per routed turn: invocation rows for t-routine and t-pro,
    # the exact terminal row for t-reused.
    executed = [("claude-haiku-4-5", 100, 0, 0), ("claude-sonnet-5-5", 300, 60, 50),
                ("sonnet", 320, 80, 50)]
    routed = sum(estimate_cost("gateway", m, i, o, c) for m, i, o, c in executed)
    ceiling = sum(estimate_cost("gateway", "sonnet", i, o, c) for _m, i, o, c in executed)
    floor = sum(estimate_cost("gateway", "haiku", i, o, c) for _m, i, o, c in executed)
    savings = summary["routing"]["savings"]
    assert savings["priced_turns"] == 3 and savings["unpriced_turns"] == 0
    assert savings["escalated_turns"] == 2
    assert savings["ceiling_model"] == "sonnet"
    assert savings["floor_model"] == "haiku"
    assert savings["routed_cost_usd"] == pytest.approx(routed, abs=1e-6)
    assert savings["ceiling_cost_usd"] == pytest.approx(ceiling, abs=1e-6)
    assert savings["floor_cost_usd"] == pytest.approx(floor, abs=1e-6)
    assert savings["saved_pct"] == round(100 * (ceiling - routed) / ceiling, 1)
    assert savings["captured_pct"] == round(100 * (ceiling - routed) / (ceiling - floor), 1)
    [route_set] = savings["route_sets"]
    assert [(c["model"], c["turns"]) for c in route_set["candidates"]] == [
        ("haiku", 1), ("sonnet", 2)]
    assert route_set["candidates"][0]["routed_cost_usd"] == pytest.approx(
        estimate_cost("gateway", "claude-haiku-4-5", 100, 0, 0), abs=1e-6)
    assert route_set["candidates"][1]["ceiling_cost_usd"] == route_set["candidates"][1]["routed_cost_usd"]
    assert route_set["candidates"][1]["fallback_turns"] == 0
    assert route_set["candidates"][1]["prices"] == {
        "input_per_mtok": 2.0, "cached_input_per_mtok": 0.1, "output_per_mtok": 10.0}


def test_dashboard_routing_savings_name_unpriced_candidates(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    conn = connect(tmp_path / "state/metrics/metrics.db")
    conn.execute("UPDATE router_decisions SET candidate_models_json='[\"haiku\",\"cx/gpt-7-nova\"]' "
                 "WHERE turn_id='t-routine'")
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    savings = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "include_dashboard": True,
    })["routing"]["savings"]
    # An unlisted candidate leaves the turn out instead of guessing a ceiling.
    assert savings["priced_turns"] == 2 and savings["unpriced_turns"] == 1
    assert savings["unpriced_models"] == ["cx/gpt-7-nova"]
    assert savings["floor_model"] == "haiku"


def test_dashboard_routing_savings_hold_helper_calls_constant(tmp_path):
    from bobi.costs import estimate_cost

    seed_dashboard(tmp_path, 1_000_000)
    conn = connect(tmp_path / "state/metrics/metrics.db")
    conn.execute("INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,model_requested,"
                 "model_selected,started_at_us,status) VALUES('i1h','t-pro',1,'gateway','haiku',"
                 "'claude-haiku-4-5',1000001,'completed')")
    conn.execute("INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,model,"
                 "measurement_source,is_estimated,input_tokens,output_tokens,cache_read_input_tokens,"
                 "observed_at_us,token_semantics_version) VALUES('u1h','invocation','t-pro','i1h','gateway',"
                 "'claude-haiku-4-5','provider_stream',0,1000,100,0,1000001,1)")
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    savings = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "include_dashboard": True,
    })["routing"]["savings"]
    helper = estimate_cost("gateway", "claude-haiku-4-5", 1000, 100, 0)
    executed = [(100, 0, 0), (300, 60, 50), (320, 80, 50)]
    ceiling = sum(estimate_cost("gateway", "sonnet", i, o, c) for i, o, c in executed)
    floor = sum(estimate_cost("gateway", "haiku", i, o, c) for i, o, c in executed)
    # The haiku helper call is real spend on every side, never re-priced as a candidate.
    assert savings["priced_turns"] == 3 and savings["unpriced_turns"] == 0
    assert savings["ceiling_cost_usd"] == pytest.approx(ceiling + helper, abs=1e-6)
    assert savings["floor_cost_usd"] == pytest.approx(floor + helper, abs=1e-6)
    assert savings["route_sets"][0]["turns"] == 3


@pytest.mark.parametrize("model", ["SONNET", "sOnNeT", "  sonnet  ", "CLAUDE-SONNET-5"])
def test_dashboard_model_substring_matches_selected_requested_and_totals(tmp_path, model):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "model": model}
    turns = queries.turns(args)["turns"]
    summary = queries.summary({**args, "include_dashboard": True, "group_by": ["model"]})
    assert {turn["turn_id"] for turn in turns} == {"t-pro", "t-reused"}
    assert summary["totals"]["input_tokens"] == 620
    assert summary["totals"]["turns"] == summary["routing"]["turns"] == len(turns)
    assert summary["totals"]["invocations"] == 2
    assert sum(group["input_tokens"] for group in summary["groups"]) == 620
    assert summary["buckets"][0]["input_tokens"] == 620
    assert summary["coverage"]["exact_invocations"] == 1
    assert summary["coverage"]["estimated_invocations"] == 1
    assert summary["coverage"]["unknown_invocations"] == 0

@pytest.mark.parametrize("model", ["", "   ", "%", "_", "\\", "absent"])
def test_dashboard_model_filter_empty_and_literal_wildcards(tmp_path, model):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "model": model}
    turns = queries.turns(args)["turns"]
    summary = queries.summary({**args, "include_dashboard": True})
    assert len(turns) == (4 if not model.strip() else 0)
    assert summary["routing"]["turns"] == len(turns)
    assert summary["totals"]["input_tokens"] == (720 if turns else None)
    assert sum(summary["coverage"][key] for key in
               ("exact_invocations", "estimated_invocations", "unknown_invocations")) == len(turns)

@pytest.mark.parametrize("dashboard", [False, True])
@pytest.mark.parametrize("model", ["ONN", "RequestedAlias", "SelectedAlias"])
def test_model_substring_filter_preserves_tool_cost_and_coverage_totals(metrics_root, dashboard, model):
    conn = connect(metrics_root / "state/metrics/metrics.db")
    conn.execute("UPDATE llm_invocations SET model_requested='vendor/RequestedAlias', "
                 "model_selected='vendor/sonnet-SelectedAlias' WHERE invocation_id='i1'")
    conn.execute("UPDATE usage_measurements SET model='vendor/sonnet-SelectedAlias' WHERE invocation_id='i1'")
    conn.execute("UPDATE cost_measurements SET model='vendor/sonnet-SelectedAlias' WHERE invocation_id='i1'")
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    queries = MetricsQueries(metrics_root)
    args = {"window_seconds": 10, "end_at": 4, "model": model, "include_dashboard": dashboard}
    summary = queries.summary(args)
    assert len(queries.turns(args)["turns"]) == summary["totals"]["turns"] == 1
    assert summary["totals"]["input_tokens"] == 100
    assert summary["totals"]["reported_cost_usd"] == 0.25
    assert summary["totals"]["tool_executions"] == 1
    assert summary["coverage"]["exact_invocations"] == 1

def test_dashboard_model_and_routing_filters_include_matching_turn_coverage(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    conn = connect(tmp_path / "state/metrics/metrics.db")
    conn.execute("UPDATE llm_invocations SET model_requested='RequestedAlias' WHERE invocation_id='i3'")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,model_selected,started_at_us,status) "
        "VALUES('second','t-unknown',1,'gateway','other',4000000,'completed')"
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    summary = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "model": "requestedalias",
        "routing": "direct", "include_dashboard": True,
    })
    assert summary["routing"]["turns"] == 1
    assert summary["coverage"]["unknown_invocations"] == 2
    excluded = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "model": "requestedalias",
        "routing": "routed", "include_dashboard": True,
    })
    assert excluded["routing"]["turns"] == 0
    assert excluded["coverage"]["unknown_invocations"] == 0

@pytest.mark.parametrize("model", ["%", "_", "\\", "%_\\", "PREFIX%"])
def test_model_like_pattern_treats_wildcards_as_literal_substrings(tmp_path, model):
    seed_dashboard(tmp_path, 1_000_000)
    conn = connect(tmp_path / "state/metrics/metrics.db")
    conn.execute("UPDATE llm_invocations SET model_requested=? WHERE invocation_id='i0'", ("prefix%_\\suffix",))
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "model": model}
    assert [turn["turn_id"] for turn in queries.turns(args)["turns"]] == ["t-routine"]
    for dashboard in (False, True):
        summary = queries.summary({**args, "include_dashboard": dashboard})
        assert summary["totals"]["input_tokens"] == 100
        assert summary["coverage"]["exact_invocations"] == 1
        assert summary["coverage"]["unknown_invocations"] == 0


def test_dashboard_keyset_filters_and_lifecycle_names(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "limit": 2}
    first = queries.turns({**args, "session_name": "worker-a"})
    second = queries.turns({**args, "session_name": "worker-a", "cursor": first["next_cursor"]})
    assert [turn["turn_id"] for turn in first["turns"] + second["turns"]] == [
        "t-unknown", "t-reused", "t-pro", "t-routine",
    ]
    assert second["next_cursor"] is None
    assert len(queries.turns({**args, "session_id": "s2"})["turns"]) == 1
    assert queries.turns({**args, "session_name": "absent"})["turns"] == []
    with pytest.raises(MetricsQueryError, match="cursor"):
        queries.turns({**args, "session_name": "other", "cursor": first["next_cursor"]})

def test_dashboard_sessions_and_canonical_turn_breakdown(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "limit": 1}
    first = queries.sessions(args)
    second = queries.sessions({**args, "cursor": first["next_cursor"]})
    assert {first["sessions"][0]["session_id"], second["sessions"][0]["session_id"]} == {"s1", "s2"}
    assert first["sessions"][0]["session_name"] == second["sessions"][0]["session_name"] == "worker-a"
    assert second["next_cursor"] is None
    detail = queries.turn({"turn_id": "t-reused", "include_breakdown": True})
    assert detail["session"]["session_id"] == "s1"
    assert detail["best_usage"][0]["input_tokens"] == 320
    assert detail["best_usage"][0]["is_estimated"] == 0
    assert detail["usage_totals"]["input_tokens"] == 320
    assert detail["invocation_usage"][0]["is_estimated"] == 1
    assert len(detail["usage_measurements"]) > len(detail["best_usage"])
    assert "raw_usage_json" not in detail["best_usage"][0]
    assert "private-task" not in json.dumps(detail)


def test_dashboard_marks_estimates_without_promoting_unknowns(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    writer = connect(tmp_path / "state/metrics/metrics.db")
    writer.execute("DELETE FROM usage_measurements WHERE measurement_id='terminal'")
    writer.commit()
    writer.close()
    result = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "include_dashboard": True,
    })
    assert result["totals"]["input_tokens"] == 1399
    assert result["buckets"][0]["is_estimated"] == 1
    assert result["totals"]["cache_write_input_tokens"] is None
    assert result["coverage"]["estimated_invocations"] == 1
    assert result["coverage"]["unknown_invocations"] == 1


def test_dashboard_read_snapshot_does_not_block_wal_writer(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)

    def read_snapshot(reader):
        original = reader.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        writer = connect(queries.db_path)
        writer.execute("UPDATE turns SET status='failed' WHERE turn_id='t-routine'")
        writer.commit()
        writer.close()
        assert reader.execute("SELECT status FROM turns WHERE turn_id='t-routine'").fetchone()[0] == "completed"
        assert original == 4
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM turns")
        return {}

    queries._run(read_snapshot)
    assert queries.turn({"turn_id": "t-routine"})["turn"]["status"] == "failed"




@pytest.fixture
def metrics_root(tmp_path):
    root = tmp_path / "agent"
    conn = connect(root / "state" / "metrics" / "metrics.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,project,started_at_us,status) "
        "VALUES('s1','manager','claude','anthropic','/private/project/path',1000000,'running')"
    )
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,ended_at_us,wall_duration_ms,status,prompt_sha256,prompt_bytes) "
        "VALUES('t1','s1',0,'user',1,2000000,3000000,1000,'completed','safehash',12)"
    )
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i1','t1',0,'anthropic','sonnet',2100000,2900000,800,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "cache_read_input_tokens,cache_write_input_tokens,cache_write_5m_input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u1','invocation','t1','i1','anthropic','sonnet','provider_stream',0,1,100,20,10,10,30,3000000)"
    )
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,invocation_id,"
        "provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c1','invocation','s1','t1','i1','anthropic','sonnet',0.25,'provider_stream',0,3000000)"
    )
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,input_sha256,input_bytes,output_sha256,output_bytes,output_estimated_tokens,"
        "started_at_us,ended_at_us,status,attribution_method,metadata_json) "
        "VALUES('x1','t1','i1','view_file','file_read','ih',10,'oh',50,13,2200000,2300000,"
        "'completed','local_tokenizer','{\"secret\":\"must-not-leak\"}')"
    )
    conn.execute(
        "INSERT INTO router_decisions(router_decision_id,turn_id,experiment_id,variant_id,"
        "assignment_status,router_name,router_version,candidate_models_json,model_selected,"
        "router_latency_ms,decided_at_us) VALUES"
        "('r1','t1','exp1','control','assigned','jev','1','[\"sonnet\"]','sonnet',4.5,2050000)"
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()
    return root


def test_turn_returns_exact_dimensions_and_no_private_metadata(metrics_root):
    result = MetricsQueries(metrics_root).turn({"turn_id": "t1"})
    assert "prompt_sha256" not in result["turn"]
    assert "input_sha256" not in result["tool_executions"][0]
    assert "output_sha256" not in result["tool_executions"][0]
    assert "assignment_key_hash" not in result["router_decisions"][0]
    assert "router_reason" not in result["router_decisions"][0]
    assert result["usage_measurements"][0]["is_estimated"] == 0
    assert result["usage_measurements"][0]["cache_write_5m_input_tokens"] == 10
    assert result["usage_measurements"][0]["cache_write_1h_input_tokens"] is None
    assert result["coverage"] == pytest.approx({
        "exact_invocations": 1,
        "estimated_invocations": 0,
        "unknown_invocations": 0,
        "events_dropped": None,
        "collector_lag_ms": None,
        "retained_from": 2000000,
        "retained_to": 3000000,
    })
    assert "metadata_json" not in result["tool_executions"][0]
    assert "safehash" not in json.dumps(result)
    assert '"ih"' not in json.dumps(result)
    assert '"oh"' not in json.dumps(result)
    assert "must-not-leak" not in json.dumps(result)

    session = MetricsQueries(metrics_root).session({"session_id": "s1"})
    assert "project" not in session["session"]
    assert "/private/project/path" not in json.dumps(session)


def test_public_query_projections_do_not_expand_with_private_schema_columns(metrics_root):
    db = metrics_root / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    conn.execute("ALTER TABLE sessions ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE turns ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE workflow_steps ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE llm_invocations ADD COLUMN future_secret TEXT")
    conn.execute("UPDATE sessions SET future_secret='session-secret'")
    conn.execute("UPDATE turns SET future_secret='turn-secret'")
    conn.execute("UPDATE llm_invocations SET future_secret='invocation-secret'")
    fill_model_keys(conn)
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    payload = {
        "session": queries.session({"session_id": "s1"}),
        "turn": queries.turn({"turn_id": "t1"}),
    }

    rendered = json.dumps(payload)
    assert "future_secret" not in rendered
    assert "session-secret" not in rendered
    assert "turn-secret" not in rendered
    assert "invocation-secret" not in rendered


# --- usage and cost precedence ------------------------------------------------
#
# One fixture turn `t1` holds invocation `i1` (sonnet) with exact invocation
# usage u1 (100 in / 30 out, cache 20/10/10) and exact invocation cost c1
# ($0.25). Each row adds measurements at another granularity, exactness or
# model alias and states which one the read model must count.

WINDOW = {"window_seconds": 10, "end_at": 4}
BY_MODEL = {**WINDOW, "group_by": ["model"]}
TURN_T1 = {"turn_id": "t1"}
_TOKEN_FIELDS = (
    "input_tokens", "uncached_input_tokens", "cache_read_input_tokens",
    "cache_write_input_tokens", "cache_write_5m_input_tokens",
    "cache_write_1h_input_tokens", "cache_write_unknown_ttl_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)


def _group(**values):
    return pytest.approx({**dict.fromkeys(_TOKEN_FIELDS), **values})


def _coverage(**values):
    base = {
        "exact_invocations": 0, "estimated_invocations": 0, "unknown_invocations": 0,
        "events_dropped": None, "collector_lag_ms": None,
        "retained_from": 2000000, "retained_to": 3000000,
    }
    return pytest.approx({**base, **values})


def _turn(turn_id, index, start, end):
    return (
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,ended_at_us,status) VALUES(?,'s1',?,'user',1,?,?,'completed')",
        (turn_id, index, start, end),
    )


def _inv(invocation_id, model="sonnet", *, turn="t1", index=1, start=2910000):
    return (
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES(?,?,?,'anthropic',?,?,?,40,'completed')",
        (invocation_id, turn, index, model, start, start + 40000),
    )


def _usage(measurement_id, scope, model, input_tokens, output_tokens, *, inv=None,
           turn="t1", estimated=0, at=3000000, cache=(None, None, None), source=None):
    return (
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,estimator_name,estimator_version,"
        "token_semantics_version,input_tokens,cache_read_input_tokens,"
        "cache_write_input_tokens,cache_write_5m_input_tokens,output_tokens,observed_at_us) "
        "VALUES(?,?,?,?,'anthropic',?,?,?,?,?,1,?,?,?,?,?,?)",
        (measurement_id, scope, turn, inv, model,
         source or ("calibrated_estimator" if estimated else "provider_stream"), estimated,
         "fixture" if estimated else None, "1" if estimated else None,
         input_tokens, *cache, output_tokens, at),
    )


def _cost(cost_id, scope, model, amount, *, inv=None, estimated=0, at=3000000):
    return (
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,"
        "invocation_id,provider,model,amount_usd,measurement_source,is_estimated,"
        "observed_at_us) VALUES(?,?,'s1','t1',?,'anthropic',?,?,?,?,?)",
        (cost_id, scope, inv, model, amount,
         "price_snapshot" if estimated else "provider_stream", estimated, at),
    )


def _pick(result, path):
    """Dotted path into a result; `groups:model,input_tokens` projects rows."""
    if ":" in path:
        key, fields = path.split(":")
        return [tuple(row[f] for f in fields.split(",")) for row in result[key]]
    for part in path.split("."):
        result = result[int(part)] if part.isdigit() else result[part]
    return result


TURN_COSTS_EXACT_ONLY = {
    "reported_cost_usd": pytest.approx(0.25), "estimated_cost_usd": None,
    "list_price_usd": pytest.approx(0.000467),
}

PRECEDENCE_CASES = [
    pytest.param(
        [],
        [
            ("summary", WINDOW, "totals.input_tokens", 100),
            ("summary", WINDOW, "totals.cache_read_input_tokens", 20),
            ("summary", WINDOW, "totals.reported_cost_usd", 0.25),
            ("summary", WINDOW, "totals.turns", 1),
            ("summary", WINDOW, "totals.invocations", 1),
            ("summary", WINDOW, "totals.tool_executions", 1),
            ("summary", {
                "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T00:00:04Z",
                "group_by": ["model", "experiment_id", "variant_id"],
                "model": "sonnet", "experiment_id": "exp1", "variant_id": "control",
            }, "groups", [_group(
                model="sonnet", experiment_id="exp1", variant_id="control",
                input_tokens=100, cache_read_input_tokens=20, cache_write_input_tokens=10,
                cache_write_5m_input_tokens=10, output_tokens=30, turns=1, invocations=1,
            )]),
        ],
        id="exact-invocation-usage-counted-once",
    ),
    pytest.param(
        [_inv("i2"), _usage("u-turn", "turn", "sonnet", 250, 70)],
        [
            ("summary", BY_MODEL, "totals.input_tokens", 250),
            ("summary", BY_MODEL, "totals.output_tokens", 70),
            ("summary", BY_MODEL, "totals.invocations", 2),
            ("summary", BY_MODEL, "groups", [_group(
                model="sonnet", input_tokens=250, output_tokens=70, turns=1, invocations=2,
            )]),
        ],
        id="partial-invocation-coverage-falls-back-to-turn",
    ),
    pytest.param(
        ["UPDATE usage_measurements SET output_tokens=0 WHERE measurement_id='u1'",
         _usage("u-turn", "turn", "sonnet", 100, 30, at=3000001, cache=(20, 10, 10))],
        [
            ("summary", WINDOW, "totals.input_tokens", 100),
            ("summary", WINDOW, "totals.output_tokens", 30),
            ("summary", WINDOW, "coverage.usage_granularity", "turn"),
        ],
        id="exact-turn-beats-exact-invocation",
    ),
    pytest.param(
        [_usage("u-turn", "turn", "sonnet", 100, 30, at=3000001, cache=(20, 10, 10))],
        [
            ("summary", WINDOW, "totals.output_tokens", 30),
            ("summary", WINDOW, "coverage.usage_granularity", "turn"),
        ],
        id="exact-turn-canonical-when-totals-match",
    ),
    pytest.param(
        [_inv("i2", "haiku"),
         _usage("u2", "invocation", "haiku", 40, 5, inv="i2"),
         _usage("u-turn", "turn", "sonnet", 100, 30, at=3000001)],
        [
            ("summary", BY_MODEL, "totals.input_tokens", 140),
            ("summary", BY_MODEL, "totals.output_tokens", 35),
            ("summary", BY_MODEL, "groups:model,input_tokens", [("haiku", 40), ("sonnet", 100)]),
        ],
        id="turn-usage-of-one-model-keeps-other-model",
    ),
    pytest.param(
        ["UPDATE llm_invocations SET model_selected='claude-current'",
         "UPDATE usage_measurements SET model='claude-current' WHERE scope='invocation'",
         _usage("u-turn", "turn", "claude-versioned", 100, 30, at=3000001)],
        [
            ("summary", BY_MODEL, "totals.input_tokens", 100),
            ("summary", BY_MODEL, "totals.output_tokens", 30),
            ("summary", BY_MODEL, "groups:model,input_tokens", [("claude-versioned", 100)]),
        ],
        id="single-invocation-model-alias-not-double-counted",
    ),
    pytest.param(
        ["UPDATE llm_invocations SET model_selected='claude-opus-5-5'",
         "UPDATE usage_measurements SET model='claude-opus-5-5' WHERE scope='invocation'",
         _inv("i2", "claude-haiku-4-5", index=2),
         _usage("u2", "invocation", "claude-haiku-4-5", 50, 10, inv="i2"),
         _usage("u-turn-opus", "turn", "claude-opus-5-5[1m]", 3000, 100, at=3000001),
         _usage("u-turn-haiku", "turn", "claude-haiku-4-5", 50, 10, at=3000001)],
        [
            ("summary", BY_MODEL, "totals.input_tokens", 3050),
            ("summary", BY_MODEL, "totals.output_tokens", 110),
            ("summary", BY_MODEL, "groups:model,input_tokens",
             [("claude-haiku-4-5", 50), ("claude-opus-5-5[1m]", 3000)]),
        ],
        id="two-model-turn-context-window-alias-not-double-counted",
    ),
    pytest.param(
        ["DELETE FROM usage_measurements",
         "UPDATE llm_invocations SET model_selected='claude-haiku-4-5-20251001'",
         _usage("u-turn", "turn", "claude-haiku-4-5-20251001", 1000, 300),
         _usage("u-transcript", "turn", "claude-haiku-4-5", 1000, 200, at=3000001,
                source="claude_transcript")],
        [
            ("summary", WINDOW, "totals.input_tokens", 1000),
            ("summary", WINDOW, "totals.output_tokens", 300),
        ],
        id="dated-and-undated-model-id-are-one-measurement",
    ),
    pytest.param(
        ["UPDATE llm_invocations SET model_selected='claude-sonnet-5-5'",
         "UPDATE usage_measurements SET model='claude-sonnet-5-5' WHERE scope='invocation'",
         _inv("i2", "claude-haiku-4-5", index=2),
         _usage("u2", "invocation", "claude-haiku-4-5", 50, 10, inv="i2"),
         _usage("u-turn-sonnet", "turn", "sonnet", 100, 30, at=3000001),
         _usage("u-turn-haiku", "turn", "claude-haiku-4-5", 50, 10, at=3000001)],
        [
            ("summary", WINDOW, "totals.input_tokens", 150),
            ("summary", WINDOW, "totals.output_tokens", 40),
        ],
        id="two-model-turn-alias-not-double-counted",
    ),
    pytest.param(
        ["DELETE FROM usage_measurements",
         _usage("u-turn", "turn", "sonnet", 1200, 300),
         _usage("u-transcript", "turn", "sonnet", 1000, 250, at=3000001,
                cache=(0, 40, 40), source="claude_transcript")],
        [
            ("summary", WINDOW, "totals.input_tokens", 1200),
            ("summary", WINDOW, "totals.output_tokens", 300),
        ],
        id="claude-terminal-total-beats-a-more-detailed-transcript",
    ),
    pytest.param(
        ["DELETE FROM usage_measurements WHERE measurement_id='u1'",
         _inv("i2"),
         _usage("u-turn-exact", "turn", "sonnet", 1000, 100),
         _usage("u-i1-est", "invocation", "sonnet", 300, 30, inv="i1", estimated=1, at=2800000),
         _usage("u-i2-est", "invocation", "sonnet", 400, 40, inv="i2", estimated=1, at=2900000)],
        [
            ("summary", WINDOW, "totals.input_tokens", 1000),
            ("summary", WINDOW, "totals.output_tokens", 100),
            ("summary", WINDOW, "totals.invocations", 2),
            ("summary", WINDOW, "coverage.usage_granularity", "turn"),
            ("summary", WINDOW, "coverage.invocation_granularity_turns", 0),
            ("summary", WINDOW, "coverage.turn_granularity_turns", 1),
        ],
        id="exact-turn-beats-complete-estimated-invocations",
    ),
    pytest.param(
        [_turn("t2", 1, 3100000, 3900000),
         _inv("i2"),
         _inv("i3", turn="t2", index=0, start=3200000),
         _inv("i4", turn="t2", index=1, start=3300000),
         _usage("u-turn-1", "turn", "sonnet", 250, 70),
         _usage("u-turn-2", "turn", "sonnet", 200, 60, turn="t2", at=3900000)],
        [
            ("summary", BY_MODEL, "totals.turns", 2),
            ("summary", BY_MODEL, "totals.invocations", 4),
            ("summary", BY_MODEL, "groups.0.invocations", 4),
        ],
        id="equal-sized-turn-fallbacks-counted-independently",
    ),
    pytest.param(
        [_inv("i2", "haiku"), _usage("u-turn", "turn", "mixed", 250, 70)],
        [
            ("summary", {**WINDOW, "model": "haiku"}, "totals.input_tokens", None),
            ("summary", {**WINDOW, "model": "haiku"}, "totals.turns", 0),
            ("summary", {**WINDOW, "model": "haiku"}, "coverage", _coverage(
                unknown_invocations=1, usage_granularity="none",
                invocation_granularity_turns=0, turn_granularity_turns=0,
            )),
        ],
        id="model-filter-does-not-split-turn-fallback",
    ),
    pytest.param(
        [_turn("t2", 1, 3100000, 3900000),
         _inv("i2", turn="t2", index=0, start=3200000),
         _usage("u-turn-2", "turn", "sonnet", 200, 60, turn="t2", at=3900000)],
        [
            ("summary", WINDOW, "totals.input_tokens", 300),
            ("summary", WINDOW, "coverage.usage_granularity", "mixed"),
            ("summary", WINDOW, "coverage.invocation_granularity_turns", 1),
            ("summary", WINDOW, "coverage.turn_granularity_turns", 1),
        ],
        id="mixed-granularity-across-turns",
    ),
    pytest.param(
        [_inv("i2", "haiku"),
         _usage("u2", "invocation", "haiku", 40, 10, inv="i2", at=2950000),
         ("INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,"
          "tool_name,tool_kind,started_at_us,status,attribution_method) "
          "VALUES('x2','t1','i2','edit_file','file_edit',2920000,'completed','unknown')", ())],
        [
            ("summary", {**WINDOW, "model": "haiku"}, "totals.input_tokens", 40),
            ("summary", {**WINDOW, "model": "haiku"}, "totals.tool_executions", 1),
        ],
        id="model-filter-counts-only-its-tools",
    ),
    pytest.param(
        [_cost("c-turn", "turn", "sonnet", 0.30)],
        [
            ("summary", WINDOW, "totals.reported_cost_usd", 0.25),
            ("turns", WINDOW, "turns.0.costs", TURN_COSTS_EXACT_ONLY),
        ],
        id="cost-one-granularity",
    ),
    pytest.param(
        [_cost("c-est", "invocation", "other-model", 0.20, inv="i1", estimated=1, at=2500000)],
        [
            ("summary", WINDOW, "totals.reported_cost_usd", 0.25),
            ("summary", WINDOW, "totals.estimated_cost_usd", None),
        ],
        id="exact-cost-suppresses-estimate-same-invocation",
    ),
    pytest.param(
        [_inv("i2", "haiku"),
         _cost("c-est-2", "invocation", "haiku", 0.05, inv="i2", estimated=1, at=2950000)],
        [
            ("summary", WINDOW, "totals.reported_cost_usd", 0.25),
            ("summary", WINDOW, "totals.estimated_cost_usd", 0.05),
            ("turns", WINDOW, "turns.0.costs", {
                **TURN_COSTS_EXACT_ONLY, "estimated_cost_usd": pytest.approx(0.05),
            }),
        ],
        id="mixed-exact-and-estimated-invocation-costs",
    ),
    pytest.param(
        ["DELETE FROM cost_measurements WHERE cost_measurement_id='c1'",
         _inv("i2"),
         _cost("c-turn-exact", "turn", "sonnet", 1.00),
         _cost("c-i1-est", "invocation", "sonnet", 0.30, inv="i1", estimated=1, at=2800000),
         _cost("c-i2-est", "invocation", "sonnet", 0.40, inv="i2", estimated=1, at=2900000)],
        [
            ("summary", WINDOW, "totals.reported_cost_usd", 1.00),
            ("summary", WINDOW, "totals.estimated_cost_usd", None),
        ],
        id="exact-turn-cost-beats-complete-estimated-invocation-costs",
    ),
    pytest.param(
        [_usage("u-est", "invocation", "other-model", 90, 20, inv="i1", estimated=1, at=2500000)],
        [
            ("turn", TURN_T1, "coverage.exact_invocations", 1),
            ("turn", TURN_T1, "coverage.estimated_invocations", 0),
            ("turn", TURN_T1, "coverage.unknown_invocations", 0),
            ("summary", WINDOW, "totals.input_tokens", 100),
        ],
        id="exact-coverage-supersedes-estimate-same-invocation",
    ),
]


@pytest.mark.parametrize(("setup", "checks"), PRECEDENCE_CASES)
def test_usage_and_cost_precedence(metrics_root, setup, checks):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    for statement in setup:
        sql, params = (statement, ()) if isinstance(statement, str) else statement
        conn.execute(sql, params)
    rekey(conn)  # setup may rename a model
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    for query, args, path, expected in checks:
        actual = _pick(getattr(queries, query)(dict(args)), path)
        if isinstance(expected, float):
            expected = pytest.approx(expected)
        assert actual == expected, (query, path)


def test_session_cursor_is_filter_bound(metrics_root):
    first = MetricsQueries(metrics_root).session({"session_id": "s1", "limit": 1})
    assert first["turns"][0]["turn_id"] == "t1"
    with pytest.raises(MetricsQueryError, match="cursor") as error:
        MetricsQueries(metrics_root).session({"session_id": "other", "limit": 1, "cursor": "bad"})
    assert error.value.code == "invalid_cursor"


def test_session_keyset_cursor_is_stable_when_an_earlier_turn_is_inserted(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,status) VALUES('t2','s1',1,'user',1,4000000,'completed')"
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    first = queries.session({"session_id": "s1", "limit": 1})
    assert [row["turn_id"] for row in first["turns"]] == ["t1"]

    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,status) VALUES('t0','s1',-1,'user',1,1000000,'completed')"
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()

    second = queries.session({
        "session_id": "s1", "limit": 1, "cursor": first["next_cursor"],
    })
    assert [row["turn_id"] for row in second["turns"]] == ["t2"]


@pytest.mark.parametrize("cursor", [False, 0, [], {}])
def test_cursor_rejects_falsy_non_string_values(metrics_root, cursor):
    with pytest.raises(MetricsQueryError) as session_error:
        MetricsQueries(metrics_root).session({
            "session_id": "s1",
            "cursor": cursor,
        })
    assert session_error.value.code == "invalid_cursor"


def test_turn_rejects_more_than_200_combined_child_rows(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.executemany(
        "INSERT INTO workflow_steps(workflow_step_id,session_id,turn_id,workflow_name,"
        "step_name,step_index,step_type,started_at_us,status) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (f"w{index}", "s1", "t1", "wf", f"step-{index}", index, "task", 2000000 + index, "completed")
            for index in range(196)
        ],
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).turn({"turn_id": "t1"})
    assert error.value.code == "query_too_large"


def test_serialized_response_is_capped_at_512_kib(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "UPDATE sessions SET session_name=? WHERE session_id='s1'",
        ("x" * (MAX_RESPONSE_BYTES + 1),),
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).session({"session_id": "s1", "include_turns": False})
    assert error.value.code == "query_too_large"


@pytest.mark.parametrize(
    "args",
    [
        {"session_id": "s1", "limit": True},
        {"session_id": "s1", "include_turns": "false"},
    ],
)
def test_session_rejects_boolean_limit_and_non_boolean_include_turns(metrics_root, args):
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).session(args)
    assert error.value.code == "bad_request"


def test_query_layer_rejects_boolean_window(metrics_root):
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).summary({"window_seconds": True})
    assert error.value.code == "invalid_range"


def test_errors_are_machine_readable(metrics_root, tmp_path):
    with pytest.raises(MetricsQueryError) as missing:
        MetricsQueries(tmp_path).turn({"turn_id": "t1"})
    assert missing.value.code == "metrics_not_ready"
    with pytest.raises(MetricsQueryError) as unknown:
        MetricsQueries(metrics_root).turn({"turn_id": "missing"})
    assert unknown.value.code == "unknown_turn"
    with pytest.raises(MetricsQueryError) as too_large:
        MetricsQueries(metrics_root).session({"session_id": "s1", "limit": MAX_ROWS + 1})
    assert too_large.value.code == "query_too_large"


@pytest.mark.parametrize("invalid_id", [True, 1, [], {}])
def test_required_ids_reject_non_string_values(metrics_root, invalid_id):
    with pytest.raises(MetricsQueryError) as session_error:
        MetricsQueries(metrics_root).session({"session_id": invalid_id})
    assert session_error.value.code == "bad_request"

    with pytest.raises(MetricsQueryError) as turn_error:
        MetricsQueries(metrics_root).turn({"turn_id": invalid_id})
    assert turn_error.value.code == "bad_request"


    with pytest.raises(MetricsQueryError) as naive_time:
        MetricsQueries(metrics_root).summary({
            "from": "2026-09-01T00:00:00",
            "to": "2026-09-01T00:01:00Z",
        })
    assert naive_time.value.code == "invalid_range"


def test_sqlite_lock_is_retryable_metrics_busy(metrics_root, monkeypatch):
    def locked(_conn):
        raise __import__("sqlite3").OperationalError("database is locked")

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root)._run(locked)
    assert error.value.code == "metrics_busy"
    assert error.value.detail == {"retry_after_ms": 250}


def test_sqlite_progress_deadline_is_a_non_retryable_refusal(metrics_root):
    queries = MetricsQueries(metrics_root, deadline_seconds=0)

    def expensive(conn):
        conn.execute(
            "WITH RECURSIVE count(value) AS ("
            "SELECT 1 UNION ALL SELECT value+1 FROM count WHERE value<1000000) "
            "SELECT SUM(value) FROM count"
        ).fetchone()
        return {"unexpected": True}

    with pytest.raises(MetricsQueryError) as error:
        queries._run(expensive)
    assert error.value.code == "query_too_slow"
    assert "deadline" in str(error.value)
    assert error.value.detail == {}


def test_python_query_processing_respects_deadline(metrics_root, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("bobi.metrics.query.time.monotonic", lambda: now[0])
    def processing(conn):
        now[0] = 2.0
        return {"late": True}
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root, deadline_seconds=1)._run(processing)
    assert error.value.code == "query_too_slow"
    assert error.value.detail == {}


def test_metrics_status_reports_database_and_reconciliation(monkeypatch, tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    try:
        migrate(conn)
    finally:
        conn.close()
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")

    result = CliRunner().invoke(metrics, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "enabled"
    assert payload["database"]["ready"] is True
    assert payload["database"]["integrity"] == "ok"
    assert payload["reconciliation"]["uncovered_turns"] == 0
    assert {"maintenance", "retention", "backup"}.isdisjoint(payload)


def test_metrics_reconcile_invokes_one_turn(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.reconcile.reconcile_turn",
        lambda root, turn_id, wait, timeout: calls.append(
            (root, turn_id, wait, timeout)
        ) or {
            "status": "done",
            "exact_measurements_recovered": 1,
            "turn_id": turn_id,
        },
    )

    result = CliRunner().invoke(
        metrics,
        ["reconcile", "--turn-id", "turn-1", "--wait", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, "turn-1", True, 60.0)]
    assert json.loads(result.output)["turn_id"] == "turn-1"


def test_metrics_commands_are_operational_only():
    assert set(metrics.commands) == {"status", "reconcile"}


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.mark.parametrize("alias", ["metrics_summary", "metrics_turn"])
def test_client_never_sends_the_operator_token_over_cleartext_to_a_remote_host(monkeypatch, alias):
    sent = []
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: sent.append(url))
    monkeypatch.setattr("bobi.admin_client.http.get", lambda url, **kwargs: sent.append(url))
    with pytest.raises(AdminClientError, match="https"):
        run_admin_command(base_url="http://events.example.com:8080", token="secret", fleet="f",
                          instance="i", alias=alias, args={"turn_id": "t1"})
    assert sent == []


def test_client_allows_cleartext_to_loopback(monkeypatch):
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: Response(
        202, {"command_id": "c1", "status": "done", "result": {"usage": {}}}))
    result = run_admin_command(base_url="http://127.0.0.1:8787", token="secret", fleet="f",
                               instance="i", alias="metrics_summary", args={})
    assert result["status"] == "done"


def test_client_maps_alias_and_polls_terminal_result(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    def get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        if url.endswith("/fleet/instances/my%20fleet/agent%2Fone"):
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        return Response(200, {
            "command_id": "c1", "status": "done", "result": {"usage_turn": {"turn": {"turn_id": "t1"}}},
        })

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com/",
        token="secret",
        fleet="my fleet",
        instance="agent/one",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
    )

    assert result["status"] == "done"
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/fleet/instances/my%20fleet/agent%2Fone")
    assert calls[1][1].endswith("/fleet/instances/my%20fleet/agent%2Fone/commands")
    assert calls[1][2]["json"] == {"command": "usage_turn", "args": {"turn_id": "t1"}}
    assert calls[1][2]["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("version", ["0.3.9", None, "development"])
def test_drilldown_rejects_unsupported_supervisor_without_post(monkeypatch, version):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda url, **kwargs: (
        calls.append(("GET", url, kwargs)) or Response(200, {"supervisor": {"version": version}})
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda *args, **kwargs: (
        calls.append(("POST", args, kwargs)) or pytest.fail("unsupported drill-down must not be posted")
    ))

    with pytest.raises(AdminClientError, match=r"0\.4\.0 or newer is required"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
        )

    assert [call[0] for call in calls] == ["GET"]


def test_summary_remains_available_without_version_preflight(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda *args, **kwargs: (
        calls.append(("GET", args, kwargs)) or pytest.fail("summary must not preflight")
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
    )

    assert result["command_id"] == "c1"
    assert [call[0] for call in calls] == ["POST"]


def test_client_wraps_transport_failure(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )

    with pytest.raises(AdminClientError, match="admin command failed: offline"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_client_rejects_non_object_response(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, []),
    )

    with pytest.raises(AdminClientError, match="non-object"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_wait_timeout_is_total_deadline_and_returns_pending(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, {"command_id": "c1", "status": "pending"}),
    )

    def get(_url, **kwargs):
        request_timeouts.append(kwargs["timeout"])
        clock["now"] += 0.4
        return Response(200, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] <= 11.25
    assert request_timeouts
    assert all(0 < request_timeout <= 1.0 for request_timeout in request_timeouts)


def test_wait_timeout_includes_drilldown_preflight_and_submission(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def get(url, **kwargs):
        request_timeouts.append(("GET", kwargs["timeout"]))
        if url.endswith("/fleet/instances/f/i"):
            clock["now"] += 0.6
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        clock["now"] += 0.1
        return Response(200, {"command_id": "c1", "status": "pending"})

    def post(_url, **kwargs):
        request_timeouts.append(("POST", kwargs["timeout"]))
        clock["now"] += 0.2
        return Response(202, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr("bobi.admin_client.http.post", post)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] == pytest.approx(11.0)
    assert request_timeouts[0] == ("GET", pytest.approx(1.0))
    assert request_timeouts[1] == ("POST", pytest.approx(0.4))
    assert request_timeouts[2] == ("GET", pytest.approx(0.2))


def test_timeout_exhausted_by_preflight_does_not_submit(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])

    def get(_url, **_kwargs):
        clock["now"] += 1.0
        return Response(200, {"supervisor": {"version": "0.4.0"}})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *_args, **_kwargs: pytest.fail("expired command must not be submitted"),
    )

    with pytest.raises(AdminClientError, match="timed out before command submission"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
            timeout=1.0,
        )


def test_cli_rejects_non_object_args_without_network():
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", "[]", "--json",
    ])
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_cli_prints_wire_envelope(monkeypatch):
    monkeypatch.setattr("bobi.admin_client.run_admin_command", lambda **kwargs: {
        "command_id": "c1", "status": "done", "result": {"usage_turn": {}},
    })
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", '{"turn_id":"t1"}', "--wait", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert '"status": "done"' in result.output


def _routed_row(turn_id, model, selected, candidates, input_tokens=1000, output_tokens=100):
    return {"turn_id": turn_id, "model": model, "model_selected": selected,
            "candidate_models_json": json.dumps(candidates), "control_model": candidates[0], "fallback_reason": None,
            "input_tokens": input_tokens, "cache_read_input_tokens": 0, "output_tokens": output_tokens}


def test_routing_savings_count_each_turn_once_across_model_rows():
    from bobi.costs import estimate_cost
    from bobi.metrics.query import _routing_savings

    candidates = ["claude-haiku-4-5", "claude-opus-5-5"]
    savings = _routing_savings([
        # One opus turn recorded under two spellings of the same model.
        _routed_row("t1", "claude-opus-5-5", "opus", candidates),
        _routed_row("t1", "claude-opus-5-5[1m]", "opus", candidates),
        # A gateway remap: no usage matches the selected model, so it all counts as routed.
        _routed_row("t2", "claude-haiku-4-5-20251001", "claude-3-5-haiku-latest", ["claude-3-5-haiku-latest", "sonnet"]),
        _routed_row("t2", "claude-haiku-4-5", "claude-3-5-haiku-latest", ["claude-3-5-haiku-latest", "sonnet"]),
    ])
    assert savings["priced_turns"] == 2
    assert [entry["turns"] for entry in savings["route_sets"]] == [1, 1]
    opus_set = next(entry for entry in savings["route_sets"] if "claude-opus-5-5" in str(entry))
    # The alias "opus" is the opus candidate, not an off-list pick.
    assert [(c["model"], c["turns"], c["off_list"]) for c in opus_set["candidates"]] == [
        ("claude-haiku-4-5", 0, False), ("claude-opus-5-5", 1, False)]
    assert opus_set["routed_cost_usd"] == opus_set["ceiling_cost_usd"] == pytest.approx(
        2 * estimate_cost("gateway", "claude-opus-5-5", 1000, 100), abs=1e-6)
    assert savings["escalated_turns"] == 1


def test_routing_savings_drop_helper_cost_of_unpriced_turns():
    from bobi.metrics.query import _routing_savings

    candidates = ["haiku", "sonnet"]
    priced = _routing_savings([_routed_row("t1", "claude-haiku-4-5", "haiku", candidates)])
    savings = _routing_savings([
        _routed_row("t1", "claude-haiku-4-5", "haiku", candidates),
        # Same candidate set, but the routed model has no list price: its helper must not leak.
        _routed_row("t2", "cx/gpt-7-nova", "cx/gpt-7-nova", candidates),
        _routed_row("t2", "claude-haiku-4-5", "cx/gpt-7-nova", candidates),
    ])
    assert savings["priced_turns"] == 1 and savings["unpriced_turns"] == 1
    assert savings["unpriced_models"] == ["cx/gpt-7-nova"]
    assert savings["ceiling_cost_usd"] == priced["ceiling_cost_usd"]


def test_routing_savings_count_fallback_reasons_per_pick():
    from bobi.metrics.query import _routing_savings

    candidates = ["haiku", "sonnet"]
    rows = [_routed_row(f"t{index}", "claude-haiku-4-5", "haiku", candidates) for index in range(3)]
    rows[0]["fallback_reason"] = rows[1]["fallback_reason"] = "policy_low_confidence"
    rows[2]["fallback_reason"] = "policy_timeout"
    [route_set] = _routing_savings(rows)["route_sets"]
    haiku, sonnet = route_set["candidates"]
    assert haiku["fallback_turns"] == 3
    assert haiku["fallback_reasons"] == {"policy_low_confidence": 2, "policy_timeout": 1}
    assert sonnet["fallback_reasons"] == {}


# --- cost does not grow with history -----------------------------------------

def _insert_once(conn, statement):
    sql, params = statement
    conn.execute(sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), params)


def _seed_history(root, *, old_turns, recent_turns, now):
    """Turns of four calls each: `old_turns` a week back, `recent_turns` in the
    last hour."""
    conn = connect(root / "state" / "metrics" / "metrics.db")
    migrate(conn)
    conn.execute("INSERT OR IGNORE INTO sessions(session_id,session_name,brain,provider,"
                 "started_at_us,status) VALUES('h','agent','claude','anthropic',0,'completed')")
    rows = [(f"old-{n}", int((now - 7 * 86400) * 1e6) + n) for n in range(old_turns)]
    rows += [(f"new-{n}", int((now - 600) * 1e6) + n) for n in range(recent_turns)]
    for turn_id, at in rows:
        conn.execute("INSERT OR IGNORE INTO turns(turn_id,session_id,turn_index,trigger_kind,"
                     "is_user_initiated,started_at_us,ended_at_us,status) "
                     "VALUES(?,'h',?,'user',1,?,?,'completed')", (turn_id, at, at, at + 10))
        for k in range(4):
            model = "claude-haiku-4-5" if k == 3 else "claude-opus-5-5"
            conn.execute("INSERT OR IGNORE INTO llm_invocations(invocation_id,turn_id,"
                         "invocation_index,provider,model_selected,started_at_us,status) "
                         "VALUES(?,?,?,'anthropic',?,?,'completed')",
                         (f"{turn_id}-{k}", turn_id, k, model, at + k))
            _insert_once(conn, _usage(f"u-{turn_id}-{k}", "invocation", model, 100, 10,
                                      inv=f"{turn_id}-{k}", turn=turn_id, at=at + k))
        _insert_once(conn, _usage(f"u-{turn_id}", "turn", "claude-opus-5-5[1m]", 300, 30,
                                  turn=turn_id, at=at + 9))
        conn.execute(
            "INSERT OR IGNORE INTO router_decisions(router_decision_id,turn_id,router_name,"
            "router_version,candidate_models_json,model_selected,router_latency_ms,"
            "decided_at_us,metadata_json) VALUES(?,?,'jev','1','[]','claude-opus-5-5',1,?,?)",
            (f"d-{turn_id}", turn_id, at,
             json.dumps({"policy": {"call_id": f"call-{turn_id}", "status": "ok"}})))
    fill_model_keys(conn)
    conn.commit()
    conn.close()


def _vm_steps(monkeypatch, run):
    """SQLite progress-handler ticks (one per 1000 VM steps) spent by `run`."""
    import bobi.metrics.store as store_module

    ticks = [0]

    class Counting(sqlite3.Connection):
        def set_progress_handler(self, handler, n):
            def counted():
                ticks[0] += 1
                return handler()
            super().set_progress_handler(counted, n)

    real = sqlite3.connect
    monkeypatch.setattr(store_module.sqlite3, "connect",
                        lambda *a, **k: real(*a, factory=Counting, **k))
    try:
        run()
    finally:
        monkeypatch.setattr(store_module.sqlite3, "connect", real)
    return ticks[0]


def test_summary_of_the_last_hour_does_not_grow_with_older_history(tmp_path, monkeypatch):
    now = time.time()
    args = {"window_seconds": 3600, "end_at": now, "include_dashboard": True}
    _seed_history(tmp_path, old_turns=200, recent_turns=5, now=now)
    queries = MetricsQueries(tmp_path, deadline_seconds=60)
    small = _vm_steps(monkeypatch, lambda: queries.summary(dict(args)))
    _seed_history(tmp_path, old_turns=1000, recent_turns=5, now=now)
    large = _vm_steps(monkeypatch, lambda: queries.summary(dict(args)))

    assert queries.summary(dict(args))["totals"]["input_tokens"] == 5 * (300 + 100)
    assert large <= small * 1.5 + 5, (small, large)


def test_turn_list_of_the_last_hour_does_not_grow_with_older_history(tmp_path, monkeypatch):
    now = time.time()
    args = {"window_seconds": 3600, "end_at": now, "limit": 50}
    _seed_history(tmp_path, old_turns=200, recent_turns=5, now=now)
    queries = MetricsQueries(tmp_path, deadline_seconds=60)
    small = _vm_steps(monkeypatch, lambda: queries.turns(dict(args)))
    _seed_history(tmp_path, old_turns=1000, recent_turns=5, now=now)
    large = _vm_steps(monkeypatch, lambda: queries.turns(dict(args)))

    assert len(queries.turns(dict(args))["turns"]) == 5
    assert large <= small * 1.5 + 5, (small, large)
