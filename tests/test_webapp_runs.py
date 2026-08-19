"""The unified runs read model (U2) and `GET /api/agents/{name}/runs`.

Three places record what an agent did — the session registry, workflow run
files, monitor run records — and this fold has to make them one list the page
can sort, tab and count. The tests below pin the parts that are judgement
rather than plumbing: which on-disk word maps to which status, when a
suspended run moves from idle to awaiting action, that one
piece of work produces exactly one row, and that `counts` keeps describing the
whole set after search and pagination have cut the payload.

`now` is always passed explicitly — a threshold test that sleeps is a test
that is slow and flaky in exchange for nothing.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from bobi.monitors import run_records
from bobi.monitors.run_records import FAILED as MONITOR_FAILED
from bobi.monitors.run_records import NOTIFIED, QUIET, MonitorRun
from bobi.webapp import server
from bobi.webapp.runs import AWAITING_ACTION_AFTER_SECONDS, build_runs

TOKEN = "runs-token-123"
NOW = 1785600000.0
MANAGER = "bobi-test-agent-director"


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def _session(install, name, *, status="completed", role="review-worker",
             pid=0, started_at=NOW - 600, terminal_at=NOW - 300,
             last_activity=NOW - 300, error="", title="", run_key="",
             model_usage=None, total_cost_usd=0.0, requested_by=None):
    d = install.sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "name": name, "session_id": f"sid-{name}", "role": role,
        "status": status, "pid": pid, "error": error,
        "started_at": started_at, "terminal_at": terminal_at,
        "last_activity": last_activity, "title": title, "run_key": run_key,
        "model_usage": model_usage or {}, "total_cost_usd": total_cost_usd,
        "requested_by": requested_by or {}, "model": "sonnet-5",
        "provider": "anthropic",
    }))


def _utc(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _workflow(install, run_id, *, status="completed", name="triage",
              started_at=NOW - 1800, completed_at=NOW - 1500,
              suspended_at_step=-1, await_event="", session_name="",
              resumed_at=0.0, aware=False):
    # aware=False writes the legacy naive-LOCAL timestamps of pre-timeutil
    # versions; aware=True writes what WorkflowRun records today (aware UTC).
    # Both shapes exist on real disks, so both must fold correctly.
    ts = _utc if aware else _iso
    runs_dir = install.state_dir / "workflow" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "workflow_name": name,
        "trigger_event": {"type": "github/issue.opened", "data": {}},
        "started_at": ts(started_at),
        "completed_at": ts(completed_at) if completed_at else "",
        "status": status, "suspended_at_step": suspended_at_step,
        "await_event": await_event, "session_name": session_name,
        "variable_scopes": {}, "repo": "", "cwd": "", "run_key": "",
        "resumed_at": ts(resumed_at) if resumed_at else "",
    }))


def _monitor(install, monitor="inbox-watch", *, outcome=QUIET, reason="",
             session_ref="", started_at=NOW - 900, ended_at=NOW - 896,
             flavor="command", script_cache_mode="", published=0):
    run = MonitorRun(
        run_id=run_records.new_run_id(monitor), monitor=monitor,
        started_at=_utc(started_at),
        ended_at=_utc(ended_at), outcome=outcome, reason=reason,
        flavor=flavor, script_cache_mode=script_cache_mode,
        session_ref=session_ref, published=published)
    run_records.record(run)
    return run


def _rows(install, **kw):
    kw.setdefault("now", NOW)
    return build_runs(install.repo_path, **kw)


def _by_key(payload):
    return {r["key"]: r for r in payload["runs"]}


# === status vocabulary ===


class TestSessionStatus:
    @pytest.mark.parametrize("recorded,expected", [
        ("running", "running"),
        ("starting", "running"),
        ("idle", "idle"),
        ("completed", "done"),
        ("done", "done"),
        ("stopped", "done"),        # over, and not a failure we can claim
        ("failed", "failed"),
        ("error", "failed"),
        ("crashed", "crashed"),
    ])
    def test_maps_the_registry_word(self, bobi_install, recorded, expected):
        _session(bobi_install, "worker", status=recorded)
        assert _by_key(_rows(bobi_install))["session:worker"]["status"] \
            == expected


class TestWorkflowStatus:
    def test_completed_is_done(self, bobi_install):
        _workflow(bobi_install, "wf-1")
        assert _by_key(_rows(bobi_install))["workflow:wf-1"]["status"] == "done"

    def test_failed_is_failed(self, bobi_install):
        _workflow(bobi_install, "wf-1", status="failed")
        assert _by_key(_rows(bobi_install))["workflow:wf-1"]["status"] \
            == "failed"

    def test_cancelled_is_closed(self, bobi_install):
        _workflow(bobi_install, "wf-1", status="cancelled")
        assert _by_key(_rows(bobi_install))["workflow:wf-1"]["status"] \
            == "closed"

    def test_waiting_is_idle_just_under_the_threshold(self, bobi_install):
        _workflow(bobi_install, "wf-1", status="waiting", completed_at=0,
                  started_at=NOW - AWAITING_ACTION_AFTER_SECONDS + 60,
                  suspended_at_step=2, await_event="pr.merged")
        assert _by_key(_rows(bobi_install))["workflow:wf-1"]["status"] == "idle"

    def test_aware_era_completed_run_folds_duration_correctly(self, bobi_install):
        _workflow(bobi_install, "wf-aware", aware=True,
                  started_at=NOW - 1800, completed_at=NOW - 1500)
        row = _by_key(_rows(bobi_install))["workflow:wf-aware"]
        assert row["status"] == "done"
        assert row["duration_seconds"] == 300.0

    def test_aware_era_waiting_run_awaits_action_at_the_threshold(self, bobi_install):
        _workflow(bobi_install, "wf-aware", aware=True, status="waiting",
                  completed_at=0,
                  started_at=NOW - AWAITING_ACTION_AFTER_SECONDS,
                  suspended_at_step=2, await_event="pr.merged")
        assert (_by_key(_rows(bobi_install))["workflow:wf-aware"]["status"]
                == "awaiting_action")

    def test_old_waiting_run_awaits_action_at_the_threshold(self, bobi_install):
        _workflow(bobi_install, "wf-1", status="waiting", completed_at=0,
                  started_at=NOW - AWAITING_ACTION_AFTER_SECONDS,
                  suspended_at_step=2, await_event="pr.merged")
        row = _by_key(_rows(bobi_install))["workflow:wf-1"]
        assert row["status"] == "awaiting_action"
        assert row["detail"]["await_event"] == "pr.merged"
        # Waiting for a human is not an error.
        assert row["error"] == ""

    def test_the_clock_runs_from_the_last_resume(self, bobi_install):
        # Resumed an hour ago, first suspended three days ago: it is waiting
        # again, not yet awaiting renewed action.
        _workflow(bobi_install, "wf-1", status="waiting", completed_at=0,
                  started_at=NOW - 3 * 86400, resumed_at=NOW - 3600,
                  suspended_at_step=2, await_event="pr.merged")
        assert _by_key(_rows(bobi_install))["workflow:wf-1"]["status"] == "idle"


class TestMonitorStatus:
    @pytest.mark.parametrize("outcome,expected", [
        (NOTIFIED, "done"), (QUIET, "done"), (MONITOR_FAILED, "failed"),
    ])
    def test_outcome_becomes_status(self, bobi_install, outcome, expected):
        _monitor(bobi_install, outcome=outcome)
        row = next(r for r in _rows(bobi_install)["runs"]
                   if r["kind"] == "monitor")
        assert row["status"] == expected

    def test_a_failed_firing_carries_its_reason(self, bobi_install):
        _monitor(bobi_install, outcome=MONITOR_FAILED,
                 reason="spawn-failed: claude CLI exited before first turn")
        row = next(r for r in _rows(bobi_install)["runs"]
                   if r["kind"] == "monitor")
        assert row["error"].startswith("spawn-failed")


# === one piece of work, one row ===

class TestSessionClaiming:
    def test_a_monitors_check_agent_is_not_also_its_own_row(self,
                                                            bobi_install):
        # The firing and the session it spawned are the same twelve seconds.
        # Listed twice they offered the same transcript from two rows and
        # printed the same tokens twice in a column read as a running total.
        _session(bobi_install, "check-agent-1", role="monitor",
                 run_key="stale-drafts", title="stale-drafts check")
        _monitor(bobi_install, "stale-drafts", session_ref="check-agent-1")
        payload = _rows(bobi_install)
        assert "session:check-agent-1" not in _by_key(payload)
        assert payload["counts"]["all"] == 1
        # ...and the surviving row still opens that transcript.
        assert payload["runs"][0]["session_id"] == "check-agent-1"

    def test_a_workflows_session_is_not_also_its_own_row(self, bobi_install):
        _session(bobi_install, "worker-3", title="copy-edit the launch post",
                 model_usage={"anthropic:claude-sonnet-4-20250514": {
                     "input_tokens": 320_000, "output_tokens": 41_000}})
        _workflow(bobi_install, "wf-1", session_name="worker-3")
        payload = _rows(bobi_install)
        assert list(_by_key(payload)) == ["workflow:wf-1"]
        # The tokens are counted once, on the row that kept them.
        assert payload["runs"][0]["tokens"] == 361_000

    def test_an_unclaimed_session_keeps_its_row(self, bobi_install):
        _session(bobi_install, "worker-3")
        _workflow(bobi_install, "wf-1", session_name="someone-else")
        assert set(_by_key(_rows(bobi_install))) == {
            "session:worker-3", "workflow:wf-1"}

    def test_a_claimed_sessions_failure_is_not_swallowed(self, bobi_install):
        # A record closes when the run's own bookkeeping finished, which is
        # not when its session ended. `notified` must not outrank a crash.
        _session(bobi_install, "check-agent-1", role="monitor",
                 status="crashed", error="agent process died")
        _monitor(bobi_install, "stale-drafts", outcome=NOTIFIED,
                 session_ref="check-agent-1")
        row = _rows(bobi_install)["runs"][0]
        assert row["kind"] == "monitor"
        assert row["status"] == "crashed"
        assert row["error"] == "agent process died"

    def test_a_claimed_session_still_running_keeps_the_row_live(
            self, bobi_install):
        _session(bobi_install, "worker-3", status="running", terminal_at=0.0)
        _workflow(bobi_install, "wf-1", session_name="worker-3")
        payload = _rows(bobi_install)
        assert payload["runs"][0]["status"] == "running"
        assert payload["counts"]["running"] == 1


# === ordering, counts, filtering ===

class TestOrdering:
    def test_live_first_then_newest_first(self, bobi_install):
        _session(bobi_install, "old", started_at=NOW - 9000)
        _session(bobi_install, "recent", started_at=NOW - 100)
        _session(bobi_install, "live", status="running", terminal_at=0.0,
                 started_at=NOW - 5000)
        assert [r["key"] for r in _rows(bobi_install)["runs"]] == [
            "session:live", "session:recent", "session:old"]


class TestCounts:
    def _seed_a_bit_of_everything(self, install):
        _session(install, "live", status="running", terminal_at=0.0)
        _session(install, "ok")
        _session(install, "bad", status="failed", error="turn errored")
        _session(install, "dead", status="crashed", error="died")
        _workflow(install, "wf-awaiting", status="waiting", completed_at=0,
                  started_at=NOW - AWAITING_ACTION_AFTER_SECONDS - 60,
                  suspended_at_step=3, await_event="pr.merged")

    def test_counts_describe_the_whole_set(self, bobi_install):
        self._seed_a_bit_of_everything(bobi_install)
        payload = _rows(bobi_install)
        assert payload["counts"] == {
            "all": 5, "running": 1, "awaiting_action": 1, "failed": 2}

    def test_counts_survive_the_limit_cutting_the_payload(self, bobi_install):
        self._seed_a_bit_of_everything(bobi_install)
        payload = _rows(bobi_install, limit=2)
        assert len(payload["runs"]) == 2
        assert payload["truncated"] is True
        assert payload["total"] == 5
        assert payload["counts"] == {
            "all": 5, "running": 1, "awaiting_action": 1, "failed": 2}

    def test_counts_survive_a_status_filter(self, bobi_install):
        self._seed_a_bit_of_everything(bobi_install)
        payload = _rows(bobi_install, status="running")
        assert [r["key"] for r in payload["runs"]] == ["session:live"]
        assert payload["counts"] == {
            "all": 5, "running": 1, "awaiting_action": 1, "failed": 2}

    def test_failed_is_the_tab_not_a_literal_match(self, bobi_install):
        # Terminal failures only; human gates have their own tab.
        self._seed_a_bit_of_everything(bobi_install)
        payload = _rows(bobi_install, status="failed")
        assert {r["status"] for r in payload["runs"]} == {
            "failed", "crashed"}

    def test_awaiting_action_has_its_own_filter(self, bobi_install):
        self._seed_a_bit_of_everything(bobi_install)
        payload = _rows(bobi_install, status="awaiting_action")
        assert [r["status"] for r in payload["runs"]] == ["awaiting_action"]

    def test_awaiting_action_sorts_after_running_before_completed(
            self, bobi_install):
        self._seed_a_bit_of_everything(bobi_install)
        statuses = [r["status"] for r in _rows(bobi_install)["runs"]]
        assert statuses[0] == "running"
        assert statuses[1] == "awaiting_action"

    def test_search_is_case_insensitive_and_reads_nested_detail(
            self, bobi_install):
        _session(bobi_install, "worker", title="Review Launch Copy")
        _workflow(bobi_install, "wf-1", status="waiting", completed_at=0,
                  started_at=NOW - AWAITING_ACTION_AFTER_SECONDS,
                  suspended_at_step=2, await_event="pr.merged")
        assert [r["key"] for r in _rows(
            bobi_install, query="LAUNCH copy")["runs"]] == ["session:worker"]
        assert [r["key"] for r in _rows(
            bobi_install, query="PR.MERGED")["runs"]] == ["workflow:wf-1"]

    def test_search_and_status_filter_before_pagination(self, bobi_install):
        _session(bobi_install, "new", title="unrelated",
                 started_at=NOW - 10)
        _session(bobi_install, "match", title="needle",
                 status="failed", error="needle failed",
                 started_at=NOW - 1000)
        payload = _rows(
            bobi_install, status="failed", query="needle", limit=1)
        assert [r["key"] for r in payload["runs"]] == ["session:match"]
        assert payload["total"] == 1

    def test_offset_returns_the_next_stable_page(self, bobi_install):
        for i in range(3):
            _session(bobi_install, f"worker-{i}", started_at=NOW - 100 * i)
        first = _rows(bobi_install, limit=2)
        second = _rows(bobi_install, limit=2, offset=2)
        assert [r["key"] for r in first["runs"]] == [
            "session:worker-0", "session:worker-1"]
        assert [r["key"] for r in second["runs"]] == ["session:worker-2"]
        assert first["total"] == second["total"] == 3
        assert first["truncated"] is True
        assert second["truncated"] is False


class TestShape:
    def test_every_row_carries_every_key(self, bobi_install):
        _session(bobi_install, "worker")
        expected = {"kind", "key", "status", "title", "origin", "started_at",
                    "duration_seconds", "tokens", "cost_usd", "est_cost_usd",
                    "error", "session_id", "run_id", "detail"}
        assert set(_rows(bobi_install)["runs"][0]) == expected

    def test_the_sort_key_never_leaves_the_fold(self, bobi_install):
        _session(bobi_install, "worker")
        assert "_started" not in _rows(bobi_install)["runs"][0]

    def test_an_empty_home_is_an_empty_list_not_an_error(self, bobi_install):
        assert _rows(bobi_install) == {
            "runs": [], "counts": {"all": 0, "running": 0,
                                    "awaiting_action": 0, "failed": 0},
            "total": 0, "offset": 0, "limit": 100, "query": "",
            "truncated": False}


# === the endpoint ===

@pytest.fixture
def client(bobi_install):
    c = TestClient(server.build_app(token=TOKEN), base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


def _get(client, path, **params):
    return client.get(path, params=params)


class TestEndpoint:
    def test_serves_the_fold(self, client, bobi_install):
        _session(bobi_install, "worker", status="running", terminal_at=0.0)
        res = _get(client, f"/api/agents/{bobi_install.agent_name}/runs")
        assert res.status_code == 200
        body = res.json()
        assert [r["key"] for r in body["runs"]] == ["session:worker"]
        assert body["counts"]["running"] == 1

    def test_status_filters_the_payload(self, client, bobi_install):
        _session(bobi_install, "worker", status="running", terminal_at=0.0)
        _session(bobi_install, "old")
        body = _get(client, f"/api/agents/{bobi_install.agent_name}/runs",
                    status="running").json()
        assert [r["key"] for r in body["runs"]] == ["session:worker"]
        assert body["counts"]["all"] == 2

    def test_limit_caps_the_payload_and_flags_it(self, client, bobi_install):
        for i in range(3):
            _session(bobi_install, f"worker-{i}", started_at=NOW - 100 * i)
        body = _get(client, f"/api/agents/{bobi_install.agent_name}/runs",
                    limit=1).json()
        assert len(body["runs"]) == 1
        assert body["truncated"] is True

    def test_default_page_contains_one_hundred_runs(self, client,
                                                     bobi_install):
        for i in range(101):
            _session(bobi_install, f"worker-{i}", started_at=NOW - i)
        body = _get(
            client, f"/api/agents/{bobi_install.agent_name}/runs").json()
        assert len(body["runs"]) == 100
        assert body["total"] == 101
        assert body["limit"] == 100
        assert body["truncated"] is True

    def test_query_and_offset_are_exposed_by_the_endpoint(self, client,
                                                           bobi_install):
        _session(bobi_install, "first", title="content review",
                 started_at=NOW)
        _session(bobi_install, "second", title="content revision",
                 started_at=NOW - 1)
        body = _get(client, f"/api/agents/{bobi_install.agent_name}/runs",
                    query="CONTENT", offset=1, limit=1).json()
        assert [r["key"] for r in body["runs"]] == ["session:second"]
        assert body["total"] == 2
        assert body["offset"] == 1
        assert body["query"] == "CONTENT"

    def test_unknown_agent_404s(self, client):
        res = _get(client, "/api/agents/does-not-exist/runs")
        assert res.status_code == 404

    def test_the_payload_is_json_safe(self, client, bobi_install):
        _session(bobi_install, "worker")
        _workflow(bobi_install, "wf-1", session_name="")
        _monitor(bobi_install)
        body = _get(client, f"/api/agents/{bobi_install.agent_name}/runs")
        json.dumps(body.json())


class TestLedgerRowContent:
    """Every run leaves a ledger entry now (#1048): the workflow row replaces
    the session row for ordinary runs, so it must carry the task and the
    failure reason, and must not double-count a session's spend."""

    def _ledger(self, install, run_id, **extra):
        runs_dir = install.state_dir / "workflow" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        base = {
            "run_id": run_id, "workflow_name": "triage",
            "trigger_event": {}, "started_at": _iso(NOW - 1800),
            "completed_at": _iso(NOW - 1500), "status": "completed",
            "suspended_at_step": -1, "await_event": "",
            "session_name": "", "variable_scopes": {}, "repo": "",
            "cwd": "", "run_key": "", "resumed_at": "",
        }
        base.update(extra)
        (runs_dir / f"{run_id}.json").write_text(json.dumps(base))

    def test_row_titles_the_task_and_carries_the_error(self, bobi_install):
        self._ledger(bobi_install, "wf-1", status="failed",
                     title="Fix moda-labs/bobi-agent#42",
                     error="Handoff missing required fields: ['pr_url']")
        row = _by_key(_rows(bobi_install))["workflow:wf-1"]
        assert row["title"] == "Fix moda-labs/bobi-agent#42"
        assert "pr_url" in row["error"]

    def test_row_without_title_falls_back_to_workflow_name(self, bobi_install):
        self._ledger(bobi_install, "wf-2")
        row = _by_key(_rows(bobi_install))["workflow:wf-2"]
        assert row["title"] == "triage"

    def test_two_entries_one_session_count_usage_once(self, bobi_install):
        # A --fresh relaunch mints a second entry on the same session name;
        # both rows citing the session's tokens would double a column the
        # reader totals by eye.
        _session(bobi_install, "wf-triage-r-42", status="completed",
                 model_usage={"anthropic:sonnet-5": {
                     "cost_usd": 1.0, "input_tokens": 500,
                     "output_tokens": 500, "cached_input_tokens": 0}},
                 total_cost_usd=1.0)
        self._ledger(bobi_install, "wf-old", status="failed",
                     session_name="wf-triage-r-42",
                     started_at=_iso(NOW - 3600))
        self._ledger(bobi_install, "wf-new", status="completed",
                     session_name="wf-triage-r-42")
        payload = _rows(bobi_install)
        rows = [r for r in payload["runs"]
                if r["key"] in ("workflow:wf-old", "workflow:wf-new")]
        assert len(rows) == 2
        assert sum(1 for r in rows if r["tokens"] > 0) <= 1
        total = sum(r["tokens"] for r in rows)
        assert total <= 1000, "the session's tokens were counted twice"
