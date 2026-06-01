"""Regression tests for the workflow-engine bugs fixed in PR #94 (issue #88).

Symptom: the ``craft_pickup_message`` node (a ``manager`` consultation node in
``workflows/issue-lifecycle.yaml``) failed with "Failed to inject into manager
session" after a 5-minute timeout, the run file was sometimes written as a
0-byte stub, and ``workflow status`` reported "0/0 nodes" for completed runs.

The fixes, and the area each set of tests below pins down:

  1. Manager inject succeeds when the session is idle.
     -> modastack/manager/session.py :: inject()
  2. Manager inject *queues* for a busy session and gives up at a bounded
     deadline instead of hanging.
     -> modastack/manager/session.py :: inject(wait_for_ready=...)
  3. A ``manager`` node's drafted text flows into the downstream ``slack.post``
     action after the node completes (the pickup-message -> Slack path), and a
     failed inject surfaces a real reason instead of an opaque failure.
     -> modastack/workflow/executor.py :: _run_manager()
  4. Run state persists as complete, parseable JSON after every node
     (never a truncated 0-byte file).
     -> modastack/workflow/state.py :: WorkflowRun.save()
  5. Variable resolution + persistence keep node counts correct, so
     ``workflow status`` shows N/M instead of 0/0.
     -> modastack/workflow/variables.py, state.py, cli.py status
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modastack.cli import main
from modastack.manager import session
from modastack.workflow.actions import ActionRegistry
from modastack.workflow.executor import ExecutorResult, WorkflowExecutor
from modastack.workflow.schema import NodeDef, NodeType, TriggerDef, WorkflowDef
from modastack.workflow.state import WorkflowRun


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_executor.py)
# ---------------------------------------------------------------------------

def _wf(nodes: dict[str, NodeDef], name: str = "test") -> WorkflowDef:
    return WorkflowDef(
        name=name, version=1,
        trigger=TriggerDef(event="test"),
        nodes=nodes,
    )


def _event(issue_id: str = "88", title: str = "Workflow engine bugs",
           repo: str = "/tmp/test", **extra) -> dict:
    data = {"issue_id": issue_id, "title": title, "repo": repo, **extra}
    return {"type": "test", "data": data}


# ===========================================================================
# Area 1: Manager inject succeeds when the session is idle
# ===========================================================================

class TestInjectWhenIdle:
    def setup_method(self):
        self._orig = (session._client, session._loop, session._state)

    def teardown_method(self):
        session._client, session._loop, session._state = self._orig

    @patch("modastack.manager.session.log_activity")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_idle_inject_succeeds_immediately(self, mock_schedule, mock_log):
        """An idle manager accepts the inject without ever polling/waiting."""
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "waiting_input"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        with patch("modastack.manager.session.time.sleep") as mock_sleep:
            assert session.inject("draft pickup message") is True
            # Idle path must not enter the busy poll loop.
            mock_sleep.assert_not_called()

        mock_schedule.assert_called_once()
        assert session.last_inject_error() == ""
        loop.close()


# ===========================================================================
# Area 2: Manager inject queues for a busy session, bounded (does not hang)
# ===========================================================================

class TestInjectWhenBusy:
    def setup_method(self):
        self._orig = (session._client, session._loop, session._state)

    def teardown_method(self):
        session._client, session._loop, session._state = self._orig

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_busy_then_idle_queues_and_injects(self, mock_schedule, mock_sleep):
        """A consultation node waits out a briefly-busy manager, then injects."""
        session._client = MagicMock()
        loop = asyncio.new_event_loop()
        session._loop = loop
        session._state = "working"

        future = MagicMock()
        future.result.return_value = None
        mock_schedule.return_value = future

        calls = {"n": 0}

        def _free_up_after_two_polls(_seconds):
            calls["n"] += 1
            if calls["n"] >= 2:
                session._state = "waiting_input"
        mock_sleep.side_effect = _free_up_after_two_polls

        with patch("modastack.manager.session.log_activity"):
            assert session.inject("draft", wait_for_ready=300) is True

        assert calls["n"] >= 2
        mock_schedule.assert_called_once()
        loop.close()

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session.asyncio.run_coroutine_threadsafe")
    def test_busy_throughout_gives_up_at_deadline_not_hangs(
        self, mock_schedule, mock_sleep
    ):
        """Core of issue #88: a manager that never frees up must make inject
        give up at the wait_for_ready deadline rather than block forever."""
        session._client = MagicMock()
        session._loop = MagicMock()
        session._state = "working"  # never flips to waiting_input

        # Drive a deterministic clock: each monotonic() reading jumps past the
        # deadline after a couple of polls, so the loop terminates without ever
        # actually sleeping in real time.
        ticks = iter([0.0, 1.0, 2.0, 999.0, 999.0, 999.0])

        with patch("modastack.manager.session.time.monotonic",
                   side_effect=lambda: next(ticks)), \
             patch("modastack.manager.session.log_activity"):
            result = session.inject("draft", wait_for_ready=5)

        assert result is False
        assert "busy" in session.last_inject_error()
        # It bailed out — never scheduled the query onto the loop.
        mock_schedule.assert_not_called()


# ===========================================================================
# Area 3: manager node output -> downstream slack.post action
# ===========================================================================

class TestManagerNodeToSlack:
    def test_manager_output_flows_into_slack_post(self, tmp_path, monkeypatch):
        """craft_pickup_message (manager) -> post_slack_pickup (slack.post):
        the drafted reply must reach the Slack action's params."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        posted = {}
        registry = ActionRegistry()
        registry.register("slack.post", lambda p: posted.update(p) or {"ok": True})

        nodes = {
            "craft_pickup_message": NodeDef(
                id="craft_pickup_message", type=NodeType.MANAGER,
                prompt="Draft a pickup message for #${{event.issue_id}}.",
                timeout=300,
            ),
            "post_slack_pickup": NodeDef(
                id="post_slack_pickup", type=NodeType.ACTION,
                action="slack.post",
                params={"channel_id": "D123",
                        "text": "${{craft_pickup_message.output}}"},
                depends_on=["craft_pickup_message"],
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        with patch("modastack.manager.session.inject", return_value=True), \
             patch("modastack.manager.session.read_last_response",
                   return_value="Picking up #88 now — starting triage."), \
             patch("modastack.manager.session.last_inject_error", return_value=""):
            ex = WorkflowExecutor(wf, run, registry=registry)
            status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["craft_pickup_message"].outputs["output"] == \
            "Picking up #88 now — starting triage."
        assert posted["text"] == "Picking up #88 now — starting triage."

    def test_manager_node_passes_wait_for_ready_equal_to_timeout(
        self, tmp_path, monkeypatch
    ):
        """The fix: a manager node queues behind a busy manager by passing
        wait_for_ready=node.timeout (instead of failing the instant it's busy)."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        nodes = {
            "consult": NodeDef(
                id="consult", type=NodeType.MANAGER,
                prompt="What now?", timeout=300,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        captured = {}

        def fake_inject(text, timeout=300, wait_for_ready=0):
            captured["timeout"] = timeout
            captured["wait_for_ready"] = wait_for_ready
            return True

        with patch("modastack.manager.session.inject", side_effect=fake_inject), \
             patch("modastack.manager.session.read_last_response", return_value="ok"), \
             patch("modastack.manager.session.last_inject_error", return_value=""):
            ex = WorkflowExecutor(wf, run)
            ex.execute()

        assert captured["timeout"] == 300
        assert captured["wait_for_ready"] == 300

    def test_manager_inject_failure_surfaces_reason(self, tmp_path, monkeypatch):
        """A failed inject raises with the concrete reason from
        last_inject_error(), not an opaque 'inject failed'."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        nodes = {
            "consult": NodeDef(
                id="consult", type=NodeType.MANAGER,
                prompt="Draft message", timeout=60,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        with patch("modastack.manager.session.inject", return_value=False), \
             patch("modastack.manager.session.read_last_response", return_value=""), \
             patch("modastack.manager.session.last_inject_error",
                   return_value="manager busy (state=working)"):
            ex = WorkflowExecutor(wf, run)
            status = ex.execute()

        assert status == ExecutorResult.FAILED
        ns = run.nodes["consult"]
        assert ns.status == "failed"
        assert "Failed to inject into manager session" in ns.error
        assert "manager busy" in ns.error


# ===========================================================================
# Area 4: run state persists as complete JSON after every node
# ===========================================================================

class TestRunStatePersistence:
    def test_every_intermediate_save_is_complete_json(self, tmp_path, monkeypatch):
        """Regression: a process killed mid-write could leave a 0-byte run
        file. With the atomic temp+rename save, the run file is valid,
        non-empty JSON after every transition during a multi-node run."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo one"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo two",
                         depends_on=["a"]),
            "c": NodeDef(id="c", type=NodeType.BASH, command="echo three",
                         depends_on=["b"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())
        path = tmp_path / f"{run.run_id}.json"

        real_save = WorkflowRun.save
        snapshots = []

        def spy_save(self):
            real_save(self)
            # After each save the on-disk file must be complete + parseable.
            snapshots.append(json.loads(path.read_text()))
            assert path.stat().st_size > 0
            # Never an orphaned temp file.
            assert not list(tmp_path.glob(".*tmp"))

        monkeypatch.setattr(WorkflowRun, "save", spy_save)

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        # Saved many times (running + completed per node, plus finalize) and
        # every snapshot parsed cleanly.
        assert len(snapshots) >= 3
        assert json.loads(path.read_text())["status"] == "completed"

    def test_concurrent_save_never_truncates(self, tmp_path, monkeypatch):
        """The temp file is renamed over the target, so a reader never sees a
        half-written file even if save() is interrupted right after."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        run = WorkflowRun.create("test", _event())
        run.node_state("a").status = "completed"
        run.node_state("a").outputs = {"stdout": "x" * 5000}
        run.save()

        path = tmp_path / f"{run.run_id}.json"
        data = json.loads(path.read_text())
        assert data["nodes"]["a"]["outputs"]["stdout"] == "x" * 5000
        assert not list(tmp_path.glob(".*tmp"))


# ===========================================================================
# Area 5: node counts are correct in status (not 0/0)
# ===========================================================================

class TestNodeCountsInStatus:
    def test_workflow_status_shows_real_node_counts(self, tmp_path, monkeypatch):
        """`workflow status` must report N/M nodes from the persisted run, not
        0/0 (the symptom when node state was never persisted)."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        run = WorkflowRun.create("issue-lifecycle", _event(issue_id="AGD-88"))
        run.node_state("a").status = "completed"
        run.node_state("b").status = "completed"
        run.node_state("c").status = "running"
        run.status = "running"
        run.save()

        result = CliRunner().invoke(main, ["workflow", "status"])
        assert result.exit_code == 0
        assert "2/3 nodes" in result.output
        assert "AGD-88" in result.output

    def test_cli_run_persists_nodes_so_counts_are_nonzero(
        self, tmp_path, monkeypatch
    ):
        """End-to-end of the 0/0 regression: a synchronous CLI run persists
        each node, so the resulting status counts are correct (not 0/0)."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        from modastack.workflow.triggers import WorkflowDispatcher

        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo hi"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo bye",
                         depends_on=["a"]),
        }
        wf = _wf(nodes, name="cli-wf")

        dispatcher = WorkflowDispatcher()
        dispatcher.workflows = [(wf, "default")]
        run = dispatcher.run_by_name("cli-wf", _event(), wait=True)

        loaded = WorkflowRun.load(run.run_id)
        completed = sum(1 for ns in loaded.nodes.values()
                        if ns.status == "completed")
        total = len(loaded.nodes)
        assert (completed, total) == (2, 2)

    def test_node_count_unaffected_by_missing_variable(self, tmp_path, monkeypatch):
        """A node that references a missing variable still completes and is
        counted — resolution falls back to '' (and warns) rather than erroring
        out and leaving the node uncounted."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        nodes = {
            "uses_missing": NodeDef(
                id="uses_missing", type=NodeType.BASH,
                command="echo 'complexity=${{triage.complexity}}'",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["uses_missing"].status == "completed"
        # Missing var resolved to empty string, not the literal placeholder.
        assert run.nodes["uses_missing"].outputs["stdout"] == "complexity="
