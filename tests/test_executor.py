"""Thorough unit tests for WorkflowExecutor.

Tests cover: bash workflows, action workflows, gate/conditional routing,
prompt nodes (mocked sub-agents), failure propagation, resume from crash,
approval suspension, variable resolution, and notification callbacks.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.workflow.actions import ActionRegistry
from modastack.workflow.executor import ExecutorResult, WorkflowExecutor
from modastack.workflow.schema import (
    BranchDef,
    ListenForDef,
    NodeDef,
    NodeType,
    TriggerDef,
    WorkflowDef,
)
from modastack.workflow.state import NodeState, WorkflowRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wf(nodes: dict[str, NodeDef], name: str = "test") -> WorkflowDef:
    return WorkflowDef(
        name=name, version=1,
        trigger=TriggerDef(event="test"),
        nodes=nodes,
    )


def _event(issue_id: str = "42", title: str = "Fix bug",
           repo: str = "/tmp/test", **extra) -> dict:
    data = {"issue_id": issue_id, "title": title, "repo": repo, **extra}
    return {"type": "test", "data": data}


# ---------------------------------------------------------------------------
# Tests: Bash nodes
# ---------------------------------------------------------------------------

class TestBashExecution:
    def test_single_bash_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {"step": NodeDef(id="step", type=NodeType.BASH, command="echo hello")}
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.status == "completed"
        assert run.nodes["step"].status == "completed"
        assert run.nodes["step"].outputs["stdout"] == "hello"

    def test_bash_chain(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo first"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo second",
                         depends_on=["a"]),
            "c": NodeDef(id="c", type=NodeType.BASH, command="echo third",
                         depends_on=["b"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        for nid in ("a", "b", "c"):
            assert run.nodes[nid].status == "completed"

    def test_bash_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "fail": NodeDef(id="fail", type=NodeType.BASH, command="exit 1"),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.FAILED
        assert run.nodes["fail"].status == "failed"
        assert run.status == "failed"

    def test_bash_with_variables(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "step": NodeDef(id="step", type=NodeType.BASH,
                            command="echo ${{event.issue_id}}"),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="BET-15"))

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["step"].outputs["stdout"] == "BET-15"

    def test_parallel_branches_independent_failure(self, tmp_path, monkeypatch):
        """Failure in one branch doesn't prevent the other from running."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "root": NodeDef(id="root", type=NodeType.BASH, command="echo ok"),
            "branch_a": NodeDef(id="branch_a", type=NodeType.BASH,
                                command="exit 1", depends_on=["root"]),
            "branch_b": NodeDef(id="branch_b", type=NodeType.BASH,
                                command="echo b_ok", depends_on=["root"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.FAILED
        assert run.nodes["branch_a"].status == "failed"
        assert run.nodes["branch_b"].status == "completed"

    def test_downstream_of_failure_skipped(self, tmp_path, monkeypatch):
        """Nodes depending on a failed node can't run and get skipped."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "fail": NodeDef(id="fail", type=NodeType.BASH, command="exit 1"),
            "after": NodeDef(id="after", type=NodeType.BASH, command="echo never",
                             depends_on=["fail"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.FAILED
        assert run.nodes["after"].status == "skipped"


# ---------------------------------------------------------------------------
# Tests: Action nodes
# ---------------------------------------------------------------------------

class TestActionExecution:
    def test_action_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        registry = ActionRegistry()
        registry.register("test.echo", lambda params: {"echoed": params["msg"]})

        nodes = {
            "step": NodeDef(id="step", type=NodeType.ACTION,
                            action="test.echo", params={"msg": "hi"}),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run, registry=registry)
        ex.execute()

        assert run.nodes["step"].outputs["echoed"] == "hi"

    def test_action_with_resolved_params(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        registry = ActionRegistry()
        registry.register("test.echo", lambda params: {"echoed": params["msg"]})

        nodes = {
            "step": NodeDef(id="step", type=NodeType.ACTION,
                            action="test.echo",
                            params={"msg": "Issue ${{event.issue_id}}"}),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="77"))

        ex = WorkflowExecutor(wf, run, registry=registry)
        ex.execute()

        assert run.nodes["step"].outputs["echoed"] == "Issue 77"


# ---------------------------------------------------------------------------
# Tests: Gate nodes
# ---------------------------------------------------------------------------

class TestGateExecution:
    def test_gate_selects_matching_branch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo spec"),
            "gate": NodeDef(
                id="gate", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "spec_path": BranchDef(when="'spec' in ${{input.stdout}}"),
                    "impl_path": BranchDef(when="'implement' in ${{input.stdout}}"),
                },
                fallback="spec_path",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["gate"].outputs["branch"] == "spec_path"

    def test_gate_uses_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo garbage"),
            "gate": NodeDef(
                id="gate", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "a": BranchDef(when="'nope' in ${{input.stdout}}"),
                },
                fallback="a",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["gate"].outputs["branch"] == "a"


# ---------------------------------------------------------------------------
# Tests: Conditional (when) nodes
# ---------------------------------------------------------------------------

class TestConditionalExecution:
    def test_when_false_skips_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo skip"),
            "maybe": NodeDef(
                id="maybe", type=NodeType.BASH, command="echo ran",
                depends_on=["input"],
                when="${{input.stdout}} == 'run'",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["maybe"].status == "skipped"

    def test_when_true_runs_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo run"),
            "maybe": NodeDef(
                id="maybe", type=NodeType.BASH, command="echo ran",
                depends_on=["input"],
                when="${{input.stdout}} == 'run'",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["maybe"].status == "completed"

    def test_gate_plus_conditional_routing(self, tmp_path, monkeypatch):
        """Full routing: gate selects a branch, when conditions filter nodes."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo implement"),
            "route": NodeDef(
                id="route", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "needs_spec": BranchDef(when="'spec' in ${{input.stdout}}"),
                    "skip_spec": BranchDef(when="'implement' in ${{input.stdout}}"),
                },
                fallback="needs_spec",
            ),
            "spec_step": NodeDef(
                id="spec_step", type=NodeType.BASH, command="echo spec_ran",
                depends_on=["route"],
                when="${{route.branch}} == 'needs_spec'",
            ),
            "impl_step": NodeDef(
                id="impl_step", type=NodeType.BASH, command="echo impl_ran",
                depends_on=["route"],
                when="${{route.branch}} == 'skip_spec'",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["route"].outputs["branch"] == "skip_spec"
        assert run.nodes["spec_step"].status == "skipped"
        assert run.nodes["impl_step"].status == "completed"
        assert run.nodes["impl_step"].outputs["stdout"] == "impl_ran"


# ---------------------------------------------------------------------------
# Tests: Prompt nodes (mocked sub-agents)
# ---------------------------------------------------------------------------

class TestPromptExecution:
    def test_prompt_node_calls_run_phase_blocking(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        from modastack.subagent import AgentResult

        nodes = {
            "triage": NodeDef(
                id="triage", type=NodeType.PROMPT,
                session="${{event.issue_id}}",
                inject="/pickup Issue #${{event.issue_id}}",
                timeout=120,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="42", repo="moda-labs/app"))

        captured = {}
        def mock_run_phase_blocking(**kwargs):
            captured.update(kwargs)
            return AgentResult(
                session_id="s1", issue_id="42", phase="pickup",
                success=True, duration_ms=5000, total_cost_usd=0.30,
            )

        with patch("modastack.subagent.run_phase_blocking", side_effect=mock_run_phase_blocking), \
             patch("modastack.workflow.executor.WorkflowExecutor._resolve_cwd",
                   return_value="/tmp/test"):
            ex = WorkflowExecutor(wf, run)
            status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["triage"].status == "completed"
        assert run.nodes["triage"].outputs["_agent_completed"] is True
        assert captured["issue_id"] == "42"
        assert captured["phase"] == "pickup"
        assert captured["timeout"] == 120

    def test_prompt_node_failure_marks_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        from modastack.subagent import AgentResult

        nodes = {
            "impl": NodeDef(
                id="impl", type=NodeType.PROMPT,
                session="${{event.issue_id}}",
                inject="/implement ${{event.issue_id}}",
                timeout=60,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        def mock_fail(**kwargs):
            return AgentResult(
                session_id="s2", issue_id="42", phase="implement",
                success=False, error="agent crashed",
            )

        with patch("modastack.subagent.run_phase_blocking", side_effect=mock_fail), \
             patch("modastack.workflow.executor.WorkflowExecutor._resolve_cwd",
                   return_value="/tmp/test"):
            ex = WorkflowExecutor(wf, run)
            status = ex.execute()

        assert status == ExecutorResult.FAILED
        assert run.nodes["impl"].status == "failed"
        assert "agent crashed" in run.nodes["impl"].error

    def test_prompt_passes_on_input_needed(self, tmp_path, monkeypatch):
        """Executor's on_input_needed is passed to run_phase_blocking."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        from modastack.subagent import AgentResult

        nodes = {
            "step": NodeDef(
                id="step", type=NodeType.PROMPT,
                session="42", inject="/implement 42", timeout=60,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        captured_handler = []
        def mock_rpb(**kwargs):
            captured_handler.append(kwargs.get("on_input_needed"))
            return AgentResult(
                session_id="s", issue_id="42", phase="implement", success=True,
            )

        def my_handler(name, inp):
            return "answer"

        with patch("modastack.subagent.run_phase_blocking", side_effect=mock_rpb), \
             patch("modastack.workflow.executor.WorkflowExecutor._resolve_cwd",
                   return_value="/tmp"):
            ex = WorkflowExecutor(wf, run, on_input_needed=my_handler)
            ex.execute()

        assert captured_handler[0] is my_handler


# ---------------------------------------------------------------------------
# Tests: Approval nodes
# ---------------------------------------------------------------------------

class TestApprovalExecution:
    def test_approval_suspends_without_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "wait": NodeDef(
                id="wait", type=NodeType.APPROVAL,
                listen_for=ListenForDef(source="github", match="approved"),
                timeout=86400,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.SUSPENDED
        assert run.nodes["wait"].status == "waiting"

    def test_approval_completes_with_matching_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "wait": NodeDef(
                id="wait", type=NodeType.APPROVAL,
                listen_for=ListenForDef(source="github", match="approved"),
                timeout=86400,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.feed_event({"source": "github", "data": {"text": "PR approved"}})
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["wait"].status == "completed"
        assert run.nodes["wait"].outputs["approved"] is True

    def test_approval_resume_after_suspend(self, tmp_path, monkeypatch):
        """Suspend, feed event, call execute() again to resume."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "before": NodeDef(id="before", type=NodeType.BASH, command="echo pre"),
            "wait": NodeDef(
                id="wait", type=NodeType.APPROVAL,
                listen_for=ListenForDef(source="github", match="approved"),
                timeout=86400,
                depends_on=["before"],
            ),
            "after": NodeDef(id="after", type=NodeType.BASH, command="echo post",
                             depends_on=["wait"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)

        # First call: runs "before", suspends on "wait"
        status = ex.execute()
        assert status == ExecutorResult.SUSPENDED
        assert run.nodes["before"].status == "completed"
        assert run.nodes["wait"].status == "waiting"
        assert "after" not in run.nodes  # not yet visited

        # Feed the approval event and resume
        ex.feed_event({"source": "github", "data": {"text": "approved"}})
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["wait"].status == "completed"
        assert run.nodes["after"].status == "completed"

    def test_approval_filters_by_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "wait": NodeDef(
                id="wait", type=NodeType.APPROVAL,
                listen_for=ListenForDef(source="github", match="approved"),
                timeout=86400,
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        # Wrong source
        ex.feed_event({"source": "slack", "data": {"text": "approved"}})
        status = ex.execute()

        assert status == ExecutorResult.SUSPENDED


# ---------------------------------------------------------------------------
# Tests: Resume from crash
# ---------------------------------------------------------------------------

class TestCrashResume:
    def test_resume_skips_completed_nodes(self, tmp_path, monkeypatch):
        """Simulate crash after first node. Resume continues from node 2."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo first"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo second",
                         depends_on=["a"]),
        }
        wf = _wf(nodes)

        # Create run with node "a" already completed (as if we crashed after it)
        run = WorkflowRun.create("test", _event())
        ns_a = run.node_state("a")
        ns_a.status = "completed"
        ns_a.outputs = {"stdout": "first", "returncode": 0}
        run.save()

        # Resume
        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["a"].status == "completed"
        assert run.nodes["b"].status == "completed"
        assert run.nodes["b"].outputs["stdout"] == "second"

    def test_resume_with_running_node_reruns(self, tmp_path, monkeypatch):
        """A node left in 'running' state (crashed mid-execution) gets re-run."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "step": NodeDef(id="step", type=NodeType.BASH, command="echo rerun"),
        }
        wf = _wf(nodes)

        run = WorkflowRun.create("test", _event())
        ns = run.node_state("step")
        ns.status = "running"
        ns.started_at = "2025-01-01T00:00:00"
        run.save()

        # Resume — node is not in a terminal state, so deps check applies.
        # But it has no deps, so it would be "pending" if we treated "running"
        # as needing re-run. Actually, "running" is not in the skip set
        # (completed/skipped/failed), and it's not "pending" or "waiting",
        # so it falls through to the execution branch.
        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["step"].outputs["stdout"] == "rerun"

    def test_load_from_disk_and_resume(self, tmp_path, monkeypatch):
        """Save a run to disk, load it back, and resume execution."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo done"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo also_done",
                         depends_on=["a"]),
        }
        wf = _wf(nodes)

        # Create, complete first node, save
        run1 = WorkflowRun.create("test", _event())
        ns_a = run1.node_state("a")
        ns_a.status = "completed"
        ns_a.outputs = {"stdout": "done", "returncode": 0}
        run1.save()

        # Load from disk
        run2 = WorkflowRun.load(run1.run_id)

        # Resume
        ex = WorkflowExecutor(wf, run2)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run2.nodes["b"].outputs["stdout"] == "also_done"


# ---------------------------------------------------------------------------
# Tests: Notification callbacks
# ---------------------------------------------------------------------------

class TestNotifications:
    def test_notifies_on_completion(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {"step": NodeDef(id="step", type=NodeType.BASH, command="echo ok")}
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="55", title="Auth fix"))

        messages = []
        ex = WorkflowExecutor(wf, run, on_notify=messages.append)
        ex.execute()

        assert len(messages) == 1
        assert "complete" in messages[0].lower()
        assert "55" in messages[0]

    def test_notifies_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {"step": NodeDef(id="step", type=NodeType.BASH, command="exit 1")}
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="66"))

        messages = []
        ex = WorkflowExecutor(wf, run, on_notify=messages.append)
        ex.execute()

        assert len(messages) == 1
        assert "failed" in messages[0].lower()
        assert "66" in messages[0]


# ---------------------------------------------------------------------------
# Tests: State persistence
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_saved_after_each_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo 1"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo 2",
                         depends_on=["a"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        # Load from disk and verify
        loaded = WorkflowRun.load(run.run_id)
        assert loaded.nodes["a"].status == "completed"
        assert loaded.nodes["b"].status == "completed"
        assert loaded.status == "completed"

    def test_failed_state_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {"fail": NodeDef(id="fail", type=NodeType.BASH, command="exit 1")}
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        loaded = WorkflowRun.load(run.run_id)
        assert loaded.nodes["fail"].status == "failed"
        assert loaded.status == "failed"


# ---------------------------------------------------------------------------
# Tests: Variable chaining across nodes
# ---------------------------------------------------------------------------

class TestVariableChaining:
    def test_output_flows_to_downstream(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "producer": NodeDef(id="producer", type=NodeType.BASH,
                                command="echo world"),
            "consumer": NodeDef(id="consumer", type=NodeType.BASH,
                                command="echo hello_${{producer.stdout}}",
                                depends_on=["producer"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["consumer"].outputs["stdout"] == "hello_world"

    def test_gate_output_used_in_condition(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo spec"),
            "gate": NodeDef(
                id="gate", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "a": BranchDef(when="'spec' in ${{input.stdout}}"),
                    "b": BranchDef(when="'impl' in ${{input.stdout}}"),
                },
                fallback="a",
            ),
            "only_a": NodeDef(
                id="only_a", type=NodeType.BASH,
                command="echo a_path",
                depends_on=["gate"],
                when="${{gate.branch}} == 'a'",
            ),
            "only_b": NodeDef(
                id="only_b", type=NodeType.BASH,
                command="echo b_path",
                depends_on=["gate"],
                when="${{gate.branch}} == 'b'",
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        ex = WorkflowExecutor(wf, run)
        ex.execute()

        assert run.nodes["only_a"].status == "completed"
        assert run.nodes["only_b"].status == "skipped"


# ---------------------------------------------------------------------------
# Tests: Complex multi-phase workflow (mini issue lifecycle)
# ---------------------------------------------------------------------------

class TestRetryFailed:
    def test_retry_resets_failed_node_and_reruns(self, tmp_path, monkeypatch):
        """Failed node can be retried: reset to pending, execute again."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        call_count = {"n": 0}
        nodes = {
            "flaky": NodeDef(id="flaky", type=NodeType.BASH,
                             command="echo success"),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        # Simulate a previous failed run
        ns = run.node_state("flaky")
        ns.status = "failed"
        ns.error = "timeout after 600s"
        run.status = "failed"
        run.save()

        # Retry
        reset = run.retry_failed()
        assert reset == ["flaky"]
        assert run.nodes["flaky"].status == "pending"
        assert run.status == "running"

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["flaky"].status == "completed"
        assert run.nodes["flaky"].outputs["stdout"] == "success"

    def test_retry_only_reruns_failed_not_completed(self, tmp_path, monkeypatch):
        """Completed nodes stay completed, only failed ones are retried."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "good": NodeDef(id="good", type=NodeType.BASH, command="echo kept"),
            "bad": NodeDef(id="bad", type=NodeType.BASH, command="echo fixed",
                           depends_on=["good"]),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event())

        # good completed, bad failed
        ns_good = run.node_state("good")
        ns_good.status = "completed"
        ns_good.outputs = {"stdout": "kept", "returncode": 0}
        ns_bad = run.node_state("bad")
        ns_bad.status = "failed"
        ns_bad.error = "oops"
        run.status = "failed"
        run.save()

        reset = run.retry_failed()
        assert reset == ["bad"]
        assert run.nodes["good"].status == "completed"

        ex = WorkflowExecutor(wf, run)
        status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert run.nodes["good"].outputs["stdout"] == "kept"
        assert run.nodes["bad"].outputs["stdout"] == "fixed"


class TestMiniLifecycle:
    def test_spawn_triage_implement_pr(self, tmp_path, monkeypatch):
        """Mini version of the issue lifecycle: spawn -> triage -> implement -> PR."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        from modastack.subagent import AgentResult

        registry = ActionRegistry()
        registry.register("session.spawn", lambda p: {"ok": True, "cwd": "/tmp"})
        registry.register("ticket.move", lambda p: {"ok": True})

        nodes = {
            "spawn": NodeDef(id="spawn", type=NodeType.ACTION,
                             action="session.spawn",
                             params={"issue_id": "${{event.issue_id}}",
                                     "repo": "${{event.repo}}"}),
            "move_ip": NodeDef(id="move_ip", type=NodeType.ACTION,
                               action="ticket.move",
                               params={"issue_id": "${{event.issue_id}}",
                                       "state": "In Progress"},
                               depends_on=["spawn"]),
            "triage": NodeDef(
                id="triage", type=NodeType.PROMPT,
                session="${{event.issue_id}}",
                inject="/pickup Issue #${{event.issue_id}}",
                timeout=120,
                depends_on=["spawn"],
            ),
            "implement": NodeDef(
                id="implement", type=NodeType.PROMPT,
                session="${{event.issue_id}}",
                inject="/implement ${{event.issue_id}}",
                timeout=300,
                depends_on=["triage"],
            ),
            "pr": NodeDef(
                id="pr", type=NodeType.PROMPT,
                session="${{event.issue_id}}",
                inject="/prepare-pr",
                timeout=120,
                depends_on=["implement"],
            ),
        }
        wf = _wf(nodes)
        run = WorkflowRun.create("test", _event(issue_id="99", repo="moda-labs/app"))

        phase_calls = []
        def mock_rpb(**kwargs):
            phase_calls.append(kwargs["phase"])
            return AgentResult(
                session_id="s", issue_id="99", phase=kwargs["phase"],
                success=True, duration_ms=1000, total_cost_usd=0.05,
            )

        with patch("modastack.subagent.run_phase_blocking", side_effect=mock_rpb), \
             patch("modastack.workflow.executor.WorkflowExecutor._resolve_cwd",
                   return_value="/tmp"):
            ex = WorkflowExecutor(wf, run, registry=registry)
            status = ex.execute()

        assert status == ExecutorResult.COMPLETED
        assert phase_calls == ["pickup", "implement", "prepare-pr"]
        for nid in ("spawn", "move_ip", "triage", "implement", "pr"):
            assert run.nodes[nid].status == "completed"
