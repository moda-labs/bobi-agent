"""The unified runs read model (U2) — the fold and its endpoint.

Two layers, mirroring U1's split: `build_runs` over a seeded home (status
mapping per source, the stalled threshold, ordering, counts vs the payload
cap) and `GET /api/agents/{name}/runs` (shape, params, unknown agent).

Every time in here is derived from a fixed `NOW` and passed explicitly — the
one derived status (`stalled`) is a clock comparison, and a test that sleeps
to cross a 24-hour threshold is not a test.
"""

import json
import time
from datetime import datetime, timezone

import yaml
from fastapi.testclient import TestClient

from bobi.monitors import run_records
from bobi.monitors.run_records import FAILED as MONITOR_FAILED
from bobi.monitors.run_records import NOTIFIED, QUIET, MonitorRun
from bobi.webapp import server
from bobi.webapp.runs import (
    CRASHED,
    DONE,
    FAILED,
    IDLE,
    RUNNING,
    STALLED,
    STALLED_AFTER_SECONDS,
    build_runs,
)
from bobi.workflow.state import WorkflowRun

TOKEN = "runs-token-123"
# A fixed clock. Far enough in the past that nothing here is ever "now".
NOW = 1_800_000_000.0
# The entry-point session name the bobi_install fixture's team resolves to.
MANAGER = "bobi-test-agent-director"


def _utc(epoch: float) -> str:
    """An aware UTC stamp — the shape monitor records are written in."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _local(epoch: float) -> str:
    """A naive local stamp — the shape `WorkflowRun` writes (`time.strftime`)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def _seed_session(sessions_dir, name, *, status, role="engineer", error="",
                  started_at=NOW, terminal_at=0.0, last_activity=0.0, pid=0,
                  title="", run_key="", requested_by=None, model_usage=None,
                  cost=0.0):
    """One session on disk, the way the registry writes it."""
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "name": name, "session_id": f"sid-{name}", "role": role,
        "status": status, "pid": pid, "error": error,
        "started_at": started_at, "terminal_at": terminal_at,
        "last_activity": last_activity, "title": title, "run_key": run_key,
        "requested_by": requested_by or {}, "model_usage": model_usage or {},
        "total_cost_usd": cost,
    }))


def _seed_workflow(run_id, *, status, name="nightly-sweep", started_at=NOW,
                   completed_at="", await_event="", suspended_at_step=-1,
                   session_name="", resumed_at="", trigger="issue.assigned"):
    run = WorkflowRun(
        run_id=run_id,
        workflow_name=name,
        trigger_event={"type": trigger, "data": {}},
        started_at=_local(started_at),
        completed_at=completed_at,
        status=status,
        suspended_at_step=suspended_at_step,
        await_event=await_event,
        session_name=session_name,
        resumed_at=resumed_at,
    )
    run.save()
    return run


def _seed_monitor_run(monitor="inbox-watch", *, outcome=QUIET, started_at=NOW,
                      ended_at=None, reason="", flavor="command",
                      script_cache_mode="", session_ref="", published=0):
    run = MonitorRun(
        run_id=run_records.new_run_id(monitor),
        monitor=monitor,
        started_at=_utc(started_at),
        ended_at=_utc(NOW if ended_at is None else ended_at),
        outcome=outcome,
        reason=reason,
        flavor=flavor,
        script_cache_mode=script_cache_mode,
        session_ref=session_ref,
        published=published,
    )
    run_records.record(run)
    return run


def _build(bobi_install, **kw):
    kw.setdefault("now", NOW)
    return build_runs(bobi_install.repo_path, **kw)


def _rows(bobi_install, **kw):
    return _build(bobi_install, **kw)["runs"]


def _only(bobi_install, **kw):
    [row] = _rows(bobi_install, **kw)
    return row


# === Sessions ===

class TestSessionRows:
    def test_active_vocabulary_reads_running(self, bobi_install):
        for status in ("starting", "running"):
            _seed_session(bobi_install.sessions_dir, f"live-{status}",
                          status=status)
        assert {r["status"] for r in _rows(bobi_install)} == {RUNNING}

    def test_idle_is_live_but_not_running(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "waiting", status="idle")
        row = _only(bobi_install)
        assert row["status"] == IDLE
        # Present, not working: idle is not counted under the RUNNING tab.
        assert _build(bobi_install)["counts"]["running"] == 0

    def test_terminal_vocabulary_maps_to_outcomes(self, bobi_install):
        sd = bobi_install.sessions_dir
        cases = {
            "completed": DONE,
            "done": DONE,           # the legacy spelling
            "failed": FAILED,
            "error": FAILED,        # what an older writer recorded
            "crashed": CRASHED,
            "cancelled": DONE,      # outside the vocabulary: over, not a failure
        }
        for status in cases:
            _seed_session(sd, f"s-{status}", status=status, terminal_at=NOW)
        got = {r["title"].removeprefix("s-"): r["status"]
               for r in _rows(bobi_install)}
        assert got == cases

    def test_failure_carries_its_error(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "boom", status="failed",
                      error="turn errored", terminal_at=NOW)
        assert _only(bobi_install)["error"] == "turn errored"

    def test_dead_pid_reads_crashed(self, bobi_install):
        # The fold reaps on read, so a session whose process died never shows
        # as running (the U1/#733 honest-status rule).
        _seed_session(bobi_install.sessions_dir, "zombie", status="running",
                      pid=999999999)
        assert _only(bobi_install)["status"] == CRASHED

    def test_manager_row_is_the_entry_point(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director")
        row = _only(bobi_install, manager_name=MANAGER)
        assert row["origin"] == "manager · entry point"
        assert row["detail"]["is_manager"] is True

    def test_subagent_origin_names_its_dispatcher(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="completed",
                      role="engineer", requested_by={"session": MANAGER},
                      terminal_at=NOW)
        assert _only(bobi_install)["origin"] == \
            f"engineer · dispatched by {MANAGER}"

    def test_undispatched_subagent_falls_back_to_the_manager(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="completed",
                      terminal_at=NOW)
        assert _only(bobi_install)["origin"] == "engineer · dispatched by manager"

    def test_monitor_session_origin_names_the_monitor(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "tick", status="completed",
                      role="monitor", run_key="inbox-watch", terminal_at=NOW)
        assert _only(bobi_install)["origin"] == "monitor · inbox-watch"

    def test_row_identity_opens_a_transcript(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="completed",
                      terminal_at=NOW)
        row = _only(bobi_install)
        assert row["key"] == "session:worker"
        # The registry entry name — what the transcript read takes — not the
        # provider-side session id.
        assert row["session_id"] == "worker"
        assert row["run_id"] == ""

    def test_duration_spans_start_to_terminal(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="completed",
                      started_at=NOW - 90, terminal_at=NOW)
        row = _only(bobi_install)
        assert row["duration_seconds"] == 90.0
        assert row["started_at"] == _utc(NOW - 90)

    def test_live_run_has_no_duration_yet(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "live", status="running",
                      started_at=NOW - 90, last_activity=NOW)
        assert _only(bobi_install)["duration_seconds"] is None

    def test_tokens_and_estimated_cost_from_model_usage(self, bobi_install):
        _seed_session(
            bobi_install.sessions_dir, "worker", status="completed",
            terminal_at=NOW,
            model_usage={"anthropic:claude-sonnet-4-20250514": {
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "cached_input_tokens": 0,
            }},
        )
        row = _only(bobi_install)
        assert row["tokens"] == 2_000_000
        assert row["cost_usd"] == 0.0
        assert row["est_cost_usd"] == 18.0        # 3.0 in + 15.0 out

    def test_reported_cost_suppresses_the_estimate(self, bobi_install):
        # An estimate must never read as a bill, and never stack on one.
        _seed_session(
            bobi_install.sessions_dir, "worker", status="completed",
            terminal_at=NOW, cost=0.25,
            model_usage={"anthropic:claude-sonnet-4-20250514": {
                "input_tokens": 1_000_000, "output_tokens": 0,
                "cached_input_tokens": 0,
            }},
        )
        row = _only(bobi_install)
        assert row["cost_usd"] == 0.25
        assert row["est_cost_usd"] == 0.0


# === Workflow runs ===

class TestWorkflowRows:
    def test_status_mapping(self, bobi_install):
        cases = {
            "completed": DONE,
            "failed": FAILED,
            "error": FAILED,
            "running": RUNNING,
            "cancelled": DONE,     # outside the vocabulary: over
        }
        for status in cases:
            _seed_workflow(f"wf-{status}", status=status, name=status)
        got = {r["title"]: r["status"] for r in _rows(bobi_install)}
        assert got == cases

    def test_fresh_suspension_is_idle_not_failed(self, bobi_install):
        for status in ("waiting", "suspended"):
            _seed_workflow(f"wf-{status}", status=status, name=status,
                           started_at=NOW - 60, await_event="pr.merged")
        assert {r["status"] for r in _rows(bobi_install)} == {IDLE}

    def test_stalled_at_the_threshold_boundary(self, bobi_install):
        # Exactly at the threshold is stalled; a second short of it is not.
        _seed_workflow("wf-old", status="waiting", name="old",
                       started_at=NOW - STALLED_AFTER_SECONDS,
                       await_event="pr.merged")
        _seed_workflow("wf-young", status="waiting", name="young",
                       started_at=NOW - STALLED_AFTER_SECONDS + 1,
                       await_event="pr.merged")
        got = {r["title"]: r["status"] for r in _rows(bobi_install)}
        assert got == {"old": STALLED, "young": IDLE}

    def test_resume_restarts_the_stall_clock(self, bobi_install):
        # Suspended a week ago but resumed a minute ago: waiting, not stalled.
        _seed_workflow("wf-1", status="waiting",
                       started_at=NOW - 7 * 24 * 3600,
                       resumed_at=_local(NOW - 60), await_event="pr.merged")
        assert _only(bobi_install)["status"] == IDLE

    def test_stalled_error_names_what_it_waits_on(self, bobi_install):
        _seed_workflow("wf-1", status="waiting",
                       started_at=NOW - STALLED_AFTER_SECONDS,
                       suspended_at_step=3, await_event="pr.merged")
        row = _only(bobi_install)
        assert row["error"] == "suspended at step 3 awaiting pr.merged"
        assert row["detail"]["resumable"] is True

    def test_stalled_without_an_await_still_says_where(self, bobi_install):
        _seed_workflow("wf-1", status="waiting",
                       started_at=NOW - STALLED_AFTER_SECONDS,
                       suspended_at_step=2)
        assert _only(bobi_install)["error"] == "suspended at step 2"

    def test_completed_run_is_not_resumable(self, bobi_install):
        _seed_workflow("wf-1", status="completed",
                       completed_at=_local(NOW))
        row = _only(bobi_install)
        assert row["error"] == ""
        assert row["detail"]["resumable"] is False

    def test_row_identity_and_origin(self, bobi_install):
        _seed_workflow("wf-1", status="running", session_name="worker",
                       trigger="issue.assigned")
        row = _only(bobi_install)
        assert row["key"] == "workflow:wf-1"
        assert row["run_id"] == "wf-1"
        assert row["session_id"] == "worker"        # its transcript
        assert row["origin"] == "workflow · on issue.assigned"

    def test_duration_spans_start_to_completion(self, bobi_install):
        _seed_workflow("wf-1", status="completed", started_at=NOW - 300,
                       completed_at=_local(NOW))
        assert _only(bobi_install)["duration_seconds"] == 300.0

    def test_tokens_come_from_the_runs_session(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="completed",
                      terminal_at=NOW, cost=0.5)
        _seed_workflow("wf-1", status="completed", session_name="worker",
                       completed_at=_local(NOW))
        [wf] = [r for r in _rows(bobi_install) if r["kind"] == "workflow"]
        assert wf["cost_usd"] == 0.5


# === Monitor runs ===

class TestMonitorRows:
    def test_notified_and_quiet_both_read_done(self, bobi_install):
        _seed_monitor_run(monitor="posted", outcome=NOTIFIED, published=2)
        _seed_monitor_run(monitor="silent", outcome=QUIET)
        assert {r["status"] for r in _rows(bobi_install)} == {DONE}

    def test_failed_firing_carries_its_reason(self, bobi_install):
        _seed_monitor_run(outcome=MONITOR_FAILED, reason="spawn-failed: no pid")
        row = _only(bobi_install)
        assert row["status"] == FAILED
        assert row["error"] == "spawn-failed: no pid"

    def test_successful_firing_never_carries_an_error(self, bobi_install):
        # `reason` on a clean run is a note ("published 2 events"), not a fault.
        _seed_monitor_run(outcome=NOTIFIED, reason="posted digest", published=2)
        row = _only(bobi_install)
        assert row["error"] == ""
        assert row["detail"]["note"] == "posted digest"

    def test_note_falls_back_to_the_published_count(self, bobi_install):
        _seed_monitor_run(outcome=NOTIFIED, published=3)
        assert _only(bobi_install)["detail"]["note"] == "published 3 event(s)"

    def test_cached_tick_has_no_transcript_to_open(self, bobi_install):
        # The $0 cached tick spawned nothing: the row can only open Details.
        run = _seed_monitor_run(outcome=QUIET, script_cache_mode="cached")
        row = _only(bobi_install)
        assert row["key"] == f"monitor:{run.run_id}"
        assert row["run_id"] == run.run_id
        assert row["session_id"] == ""
        assert row["detail"]["script_cache_mode"] == "cached"

    def test_out_of_band_firing_points_at_its_session(self, bobi_install):
        _seed_monitor_run(outcome=QUIET, flavor="description",
                          session_ref="check-agent-1")
        row = _only(bobi_install)
        assert row["session_id"] == "check-agent-1"
        assert row["detail"]["flavor"] == "description"

    def test_duration_and_title(self, bobi_install):
        _seed_monitor_run(monitor="inbox-watch", started_at=NOW - 12,
                          ended_at=NOW)
        row = _only(bobi_install)
        assert row["title"] == "inbox-watch"
        assert row["duration_seconds"] == 12.0

    def test_origin_carries_the_schedule(self, bobi_install):
        (bobi_install.repo_path / "package" / "monitors.yaml").write_text(
            yaml.dump({"monitors": [
                {"name": "inbox-watch", "interval": "15m",
                 "command": "true", "description": "watch the inbox"},
            ]}))
        _seed_monitor_run(monitor="inbox-watch")
        assert _only(bobi_install)["origin"] == "monitor · every 15m"

    def test_origin_survives_an_unreadable_registry(self, bobi_install):
        # The schedule is a sub-line; a registry that will not load costs the
        # sub-line, never the row.
        (bobi_install.repo_path / "package" / "monitors.yaml").write_text(
            "{{ not yaml")
        _seed_monitor_run(monitor="inbox-watch")
        assert _only(bobi_install)["origin"] == "monitor"


# === The fold: ordering, counts, filters ===

class TestOrderingAndCounts:
    def _seed_mixed(self, bobi_install):
        """One of everything, deliberately out of order on disk."""
        sd = bobi_install.sessions_dir
        _seed_session(sd, "old-done", status="completed",
                      started_at=NOW - 5000, terminal_at=NOW - 4000)
        _seed_session(sd, "recent-fail", status="failed", error="boom",
                      started_at=NOW - 100, terminal_at=NOW - 50)
        _seed_session(sd, "live", status="running", started_at=NOW - 3000)
        _seed_workflow("wf-stalled", status="waiting", name="stalled-wf",
                       started_at=NOW - STALLED_AFTER_SECONDS,
                       await_event="pr.merged")
        _seed_monitor_run(monitor="inbox-watch", outcome=MONITOR_FAILED,
                          reason="command exited 3", started_at=NOW - 200)

    def test_live_first_then_newest_first(self, bobi_install):
        self._seed_mixed(bobi_install)
        rows = _rows(bobi_install)
        # The running session leads despite being older than two finished rows.
        assert rows[0]["title"] == "live"
        assert [r["title"] for r in rows[1:]] == [
            "recent-fail", "inbox-watch", "old-done", "stalled-wf",
        ]

    def test_counts_cover_every_source(self, bobi_install):
        self._seed_mixed(bobi_install)
        # failed = the session, the monitor firing, and the stalled workflow.
        assert _build(bobi_install)["counts"] == {
            "all": 5, "running": 1, "failed": 3,
        }

    def test_sort_key_never_reaches_the_payload(self, bobi_install):
        self._seed_mixed(bobi_install)
        assert all("_started" not in r for r in _rows(bobi_install))

    def test_failed_is_the_tab_not_a_literal_match(self, bobi_install):
        self._seed_mixed(bobi_install)
        rows = _rows(bobi_install, status=FAILED)
        assert {r["status"] for r in rows} == {FAILED, STALLED}
        assert sorted(r["title"] for r in rows) == [
            "inbox-watch", "recent-fail", "stalled-wf",
        ]

    def test_failed_tab_includes_crashed(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "zombie", status="running",
                      pid=999999999)
        [row] = _rows(bobi_install, status=FAILED)
        assert row["status"] == CRASHED

    def test_running_tab_excludes_idle(self, bobi_install):
        sd = bobi_install.sessions_dir
        _seed_session(sd, "live", status="running")
        _seed_session(sd, "waiting", status="idle")
        assert [r["title"] for r in _rows(bobi_install, status=RUNNING)] == \
            ["live"]

    def test_counts_describe_the_whole_set_when_limit_cuts(self, bobi_install):
        self._seed_mixed(bobi_install)
        body = _build(bobi_install, limit=2)
        assert len(body["runs"]) == 2
        assert body["truncated"] is True
        # The tab counts stay true even though the payload is cut.
        assert body["counts"] == {"all": 5, "running": 1, "failed": 3}

    def test_truncated_flips_at_the_cap(self, bobi_install):
        self._seed_mixed(bobi_install)
        assert _build(bobi_install, limit=5)["truncated"] is False
        assert _build(bobi_install, limit=4)["truncated"] is True

    def test_counts_ignore_the_status_filter(self, bobi_install):
        self._seed_mixed(bobi_install)
        body = _build(bobi_install, status=RUNNING)
        assert len(body["runs"]) == 1
        assert body["counts"] == {"all": 5, "running": 1, "failed": 3}

    def test_empty_home_is_an_empty_list_not_an_error(self, bobi_install):
        assert _build(bobi_install) == {
            "runs": [], "counts": {"all": 0, "running": 0, "failed": 0},
            "truncated": False,
        }

    def test_reading_never_creates_state_directories(self, bobi_install):
        # A reader must not mkdir inside someone else's runtime tree.
        _build(bobi_install)
        state = bobi_install.state_dir
        assert not (state / "workflow" / "runs").exists()
        assert not (state / "monitor_runs").exists()

    def test_every_row_carries_every_field(self, bobi_install):
        # Render code branches on value, never on key presence.
        self._seed_mixed(bobi_install)
        expected = {"kind", "key", "status", "title", "origin", "started_at",
                    "duration_seconds", "tokens", "cost_usd", "est_cost_usd",
                    "error", "session_id", "run_id", "detail"}
        for row in _rows(bobi_install):
            assert set(row) == expected


# === The endpoint ===

def _client():
    app = server.build_app(token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


class TestRunsEndpoint:
    def _get(self, bobi_install, **params):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/runs",
                          params=params)
        assert r.status_code == 200
        return r.json()

    def test_empty_agent(self, bobi_install):
        assert self._get(bobi_install) == {
            "runs": [], "counts": {"all": 0, "running": 0, "failed": 0},
            "truncated": False,
        }

    def test_serves_the_merged_list(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="failed",
                      error="boom", started_at=NOW - 100, terminal_at=NOW)
        _seed_workflow("wf-1", status="running", name="nightly-sweep")
        _seed_monitor_run(monitor="inbox-watch", outcome=NOTIFIED, published=1)
        body = self._get(bobi_install)
        assert {r["kind"] for r in body["runs"]} == {
            "session", "workflow", "monitor"}
        assert body["counts"] == {"all": 3, "running": 1, "failed": 1}
        assert body["truncated"] is False

    def test_status_param_filters_the_payload(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "worker", status="failed",
                      error="boom", terminal_at=NOW)
        _seed_session(bobi_install.sessions_dir, "live", status="running")
        body = self._get(bobi_install, status=FAILED)
        assert [r["title"] for r in body["runs"]] == ["worker"]
        assert body["counts"]["all"] == 2

    def test_limit_param_caps_the_payload(self, bobi_install):
        for i in range(3):
            _seed_session(bobi_install.sessions_dir, f"s{i}",
                          status="completed", started_at=NOW - i,
                          terminal_at=NOW)
        body = self._get(bobi_install, limit=1)
        assert len(body["runs"]) == 1
        assert body["truncated"] is True
        assert body["counts"]["all"] == 3

    def test_manager_row_is_flagged_through_the_endpoint(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director")
        [row] = self._get(bobi_install)["runs"]
        assert row["detail"]["is_manager"] is True

    def test_unknown_agent_404s(self, bobi_install):
        r = _client().get("/api/agents/nope/runs")
        assert r.status_code == 404
        assert r.json() == {"error": "unknown agent"}

    def test_requires_the_token(self, bobi_install):
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://127.0.0.1")
        r = c.get(f"/api/agents/{bobi_install.agent_name}/runs")
        assert r.status_code == 403
