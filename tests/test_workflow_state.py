"""Unit tests for WorkflowRun and NodeState — persistence, querying, retry."""

import json
import time
from unittest.mock import patch

import pytest

from modastack.workflow.state import WorkflowRun, NodeState


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Redirect _runs_dir to a temp directory for test isolation."""
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr("modastack.workflow.state._runs_dir", lambda: d)
    return d


def _make_run(runs_dir, run_id="abc123", workflow_name="test-wf",
              status="running", issue_id="", await_event="", **overrides):
    """Helper to create and save a WorkflowRun."""
    trigger = {"type": "test", "data": {}}
    if issue_id:
        trigger["data"]["issue_id"] = issue_id
    run = WorkflowRun(
        run_id=run_id,
        workflow_name=workflow_name,
        trigger_event=trigger,
        status=status,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        await_event=await_event,
        issue_id=issue_id,
        **overrides,
    )
    run.save()
    return run


# ---------------------------------------------------------------------------
# NodeState
# ---------------------------------------------------------------------------

class TestNodeState:
    def test_defaults(self):
        ns = NodeState()
        assert ns.status == "pending"
        assert ns.started_at == ""
        assert ns.completed_at == ""
        assert ns.outputs == {}
        assert ns.error == ""

    def test_custom_values(self):
        ns = NodeState(status="completed", error="oops")
        assert ns.status == "completed"
        assert ns.error == "oops"


# ---------------------------------------------------------------------------
# WorkflowRun.create
# ---------------------------------------------------------------------------

class TestWorkflowRunCreate:
    def test_create_sets_fields(self):
        event = {"type": "issue.assigned", "data": {"issue_id": "42"}}
        run = WorkflowRun.create("lifecycle", event)
        assert run.workflow_name == "lifecycle"
        assert run.trigger_event == event
        assert run.status == "running"
        assert run.started_at != ""
        assert len(run.run_id) == 8

    def test_create_unique_ids(self):
        event = {"type": "test"}
        run1 = WorkflowRun.create("wf", event)
        run2 = WorkflowRun.create("wf", event)
        assert run1.run_id != run2.run_id


# ---------------------------------------------------------------------------
# WorkflowRun.save / load — round-trip
# ---------------------------------------------------------------------------

class TestSaveLoad:
    def test_round_trip(self, runs_dir):
        run = _make_run(runs_dir, run_id="rt1", workflow_name="lifecycle")
        loaded = WorkflowRun.load("rt1")
        assert loaded.run_id == "rt1"
        assert loaded.workflow_name == "lifecycle"
        assert loaded.status == "running"

    def test_round_trip_with_nodes(self, runs_dir):
        run = _make_run(runs_dir, run_id="rt2")
        run.nodes["step1"] = NodeState(status="completed", outputs={"key": "val"})
        run.nodes["step2"] = NodeState(status="failed", error="timeout")
        run.save()
        loaded = WorkflowRun.load("rt2")
        assert loaded.nodes["step1"].status == "completed"
        assert loaded.nodes["step1"].outputs == {"key": "val"}
        assert loaded.nodes["step2"].error == "timeout"

    def test_save_is_atomic(self, runs_dir):
        run = _make_run(runs_dir, run_id="atomic")
        path = runs_dir / "atomic.json"
        tmp_path = runs_dir / ".atomic.json.tmp"
        assert path.exists()
        assert not tmp_path.exists()

    def test_load_tolerates_missing_optional_fields(self, runs_dir):
        path = runs_dir / "minimal.json"
        path.write_text(json.dumps({
            "run_id": "minimal",
            "workflow_name": "wf",
            "trigger_event": {},
        }))
        loaded = WorkflowRun.load("minimal")
        assert loaded.run_id == "minimal"
        assert loaded.status == "running"
        assert loaded.suspended_at_step == -1
        assert loaded.variable_scopes == {}

    def test_round_trip_preserves_all_fields(self, runs_dir):
        run = _make_run(
            runs_dir, run_id="full",
            session_name="session-1",
            repo="moda-labs/modastack",
            cwd="/tmp/worktree",
            issue_id="42",
        )
        run.variable_scopes = {"handoff": {"complexity": "medium"}}
        run.suspended_at_step = 3
        run.await_event = "approval"
        run.save()
        loaded = WorkflowRun.load("full")
        assert loaded.session_name == "session-1"
        assert loaded.repo == "moda-labs/modastack"
        assert loaded.cwd == "/tmp/worktree"
        assert loaded.issue_id == "42"
        assert loaded.variable_scopes == {"handoff": {"complexity": "medium"}}
        assert loaded.suspended_at_step == 3
        assert loaded.await_event == "approval"


# ---------------------------------------------------------------------------
# WorkflowRun.node_state
# ---------------------------------------------------------------------------

class TestNodeStateAccess:
    def test_creates_new_node_on_access(self):
        run = WorkflowRun.create("wf", {})
        ns = run.node_state("step1")
        assert ns.status == "pending"
        assert "step1" in run.nodes

    def test_returns_existing_node(self):
        run = WorkflowRun.create("wf", {})
        run.nodes["step1"] = NodeState(status="completed")
        ns = run.node_state("step1")
        assert ns.status == "completed"


# ---------------------------------------------------------------------------
# WorkflowRun.find_active
# ---------------------------------------------------------------------------

class TestFindActive:
    def test_finds_running_run(self, runs_dir):
        _make_run(runs_dir, run_id="active1", status="running",
                  issue_id="42")
        found = WorkflowRun.find_active("test-wf", "42")
        assert found is not None
        assert found.run_id == "active1"

    def test_finds_waiting_run(self, runs_dir):
        _make_run(runs_dir, run_id="waiting1", status="waiting",
                  issue_id="42")
        found = WorkflowRun.find_active("test-wf", "42")
        assert found is not None
        assert found.run_id == "waiting1"

    def test_skips_completed(self, runs_dir):
        _make_run(runs_dir, run_id="done1", status="completed",
                  issue_id="42")
        assert WorkflowRun.find_active("test-wf", "42") is None

    def test_skips_wrong_workflow(self, runs_dir):
        _make_run(runs_dir, run_id="wrong1", status="running",
                  issue_id="42", workflow_name="other-wf")
        assert WorkflowRun.find_active("test-wf", "42") is None

    def test_skips_wrong_issue(self, runs_dir):
        _make_run(runs_dir, run_id="wrong2", status="running",
                  issue_id="99")
        assert WorkflowRun.find_active("test-wf", "42") is None

    def test_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state._runs_dir",
                            lambda: tmp_path / "nonexistent")
        assert WorkflowRun.find_active("wf", "42") is None

    def test_tolerates_corrupt_json(self, runs_dir):
        (runs_dir / "corrupt.json").write_text("{bad json")
        _make_run(runs_dir, run_id="good", status="running", issue_id="42")
        found = WorkflowRun.find_active("test-wf", "42")
        assert found is not None
        assert found.run_id == "good"


# ---------------------------------------------------------------------------
# WorkflowRun.find_waiting
# ---------------------------------------------------------------------------

class TestFindWaiting:
    def test_finds_by_await_event(self, runs_dir):
        _make_run(runs_dir, run_id="w1", status="waiting",
                  await_event="approval", issue_id="42")
        found = WorkflowRun.find_waiting("approval")
        assert found is not None
        assert found.run_id == "w1"

    def test_filters_by_issue_id(self, runs_dir):
        _make_run(runs_dir, run_id="w2", status="waiting",
                  await_event="approval", issue_id="42")
        _make_run(runs_dir, run_id="w3", status="waiting",
                  await_event="approval", issue_id="99")
        found = WorkflowRun.find_waiting("approval", issue_id="99")
        assert found is not None
        assert found.run_id == "w3"

    def test_skips_non_waiting(self, runs_dir):
        _make_run(runs_dir, run_id="running", status="running",
                  await_event="approval")
        assert WorkflowRun.find_waiting("approval") is None

    def test_skips_wrong_event(self, runs_dir):
        _make_run(runs_dir, run_id="wrong", status="waiting",
                  await_event="deploy")
        assert WorkflowRun.find_waiting("approval") is None

    def test_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state._runs_dir",
                            lambda: tmp_path / "nonexistent")
        assert WorkflowRun.find_waiting("approval") is None


# ---------------------------------------------------------------------------
# WorkflowRun.find_completed
# ---------------------------------------------------------------------------

class TestFindCompleted:
    def test_finds_completed_run(self, runs_dir):
        _make_run(runs_dir, run_id="done", status="completed",
                  issue_id="42")
        found = WorkflowRun.find_completed("test-wf", "42")
        assert found is not None
        assert found.run_id == "done"

    def test_skips_running(self, runs_dir):
        _make_run(runs_dir, run_id="running", status="running",
                  issue_id="42")
        assert WorkflowRun.find_completed("test-wf", "42") is None

    def test_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state._runs_dir",
                            lambda: tmp_path / "nonexistent")
        assert WorkflowRun.find_completed("wf", "42") is None


# ---------------------------------------------------------------------------
# WorkflowRun.list_runs
# ---------------------------------------------------------------------------

class TestListRuns:
    def test_lists_all_runs(self, runs_dir):
        _make_run(runs_dir, run_id="r1", status="running")
        _make_run(runs_dir, run_id="r2", status="completed")
        _make_run(runs_dir, run_id="r3", status="waiting")
        runs = WorkflowRun.list_runs()
        assert len(runs) == 3

    def test_filters_by_status(self, runs_dir):
        _make_run(runs_dir, run_id="r1", status="running")
        _make_run(runs_dir, run_id="r2", status="completed")
        runs = WorkflowRun.list_runs(status="completed")
        assert len(runs) == 1
        assert runs[0].run_id == "r2"

    def test_empty_dir_returns_empty(self, runs_dir):
        assert WorkflowRun.list_runs() == []

    def test_returns_empty_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state._runs_dir",
                            lambda: tmp_path / "nonexistent")
        assert WorkflowRun.list_runs() == []

    def test_tolerates_corrupt_files(self, runs_dir):
        (runs_dir / "bad.json").write_text("not json")
        _make_run(runs_dir, run_id="good", status="running")
        runs = WorkflowRun.list_runs()
        assert len(runs) == 1
        assert runs[0].run_id == "good"


# ---------------------------------------------------------------------------
# WorkflowRun.retry_failed
# ---------------------------------------------------------------------------

class TestRetryFailed:
    def test_resets_failed_nodes(self, runs_dir):
        run = _make_run(runs_dir, run_id="retry1")
        run.nodes["s1"] = NodeState(status="completed")
        run.nodes["s2"] = NodeState(status="failed", error="timeout")
        run.nodes["s3"] = NodeState(status="failed", error="crash")
        run.status = "failed"
        run.save()

        reset = run.retry_failed()
        assert set(reset) == {"s2", "s3"}
        assert run.nodes["s1"].status == "completed"
        assert run.nodes["s2"].status == "pending"
        assert run.nodes["s2"].error == ""
        assert run.nodes["s3"].status == "pending"
        assert run.status == "running"
        assert run.completed_at == ""

    def test_no_failed_nodes_returns_empty(self, runs_dir):
        run = _make_run(runs_dir, run_id="retry2")
        run.nodes["s1"] = NodeState(status="completed")
        run.save()
        reset = run.retry_failed()
        assert reset == []

    def test_retry_persists_to_disk(self, runs_dir):
        run = _make_run(runs_dir, run_id="retry3")
        run.nodes["s1"] = NodeState(status="failed", error="err")
        run.status = "failed"
        run.save()

        run.retry_failed()
        loaded = WorkflowRun.load("retry3")
        assert loaded.nodes["s1"].status == "pending"
        assert loaded.status == "running"
