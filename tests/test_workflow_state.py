"""Unit tests for WorkflowRun — persistence and querying."""

import json
import time
from unittest.mock import patch

import pytest

from modastack.workflow.state import WorkflowRun


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


