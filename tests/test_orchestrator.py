"""Unit tests for the workflow orchestrator — schema parsing, handoff
validation, route evaluation, step sequencing, and event emission."""

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from modastack.workflow.schema import (
    Workflow, StepDef, HandoffContract, load_workflow,
)
from modastack.workflow.orchestrator import (
    _build_step_prompt, _read_handoff, _validate_handoff, run_workflow,
)


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

class TestSchemaLoad:
    def test_load_simple_workflow(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: test-wf
            trigger: task.assigned
            steps:
              - name: greet
                prompt: "Say hello"
                handoff:
                  required: [greeting]
                timeout: 60
        """))
        wf = load_workflow(f)
        assert wf.name == "test-wf"
        assert wf.trigger == "task.assigned"
        assert len(wf.steps) == 1
        assert wf.steps[0].name == "greet"
        assert wf.steps[0].prompt.strip() == "Say hello"
        assert wf.steps[0].handoff.required == ["greeting"]
        assert wf.steps[0].timeout == 60

    def test_load_route_step(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: route-test
            steps:
              - name: check
                if: "needs_spec == true"
                goto: spec
                else: implement
        """))
        wf = load_workflow(f)
        step = wf.steps[0]
        assert step.condition == "needs_spec == true"
        assert step.goto == "spec"
        assert step.else_goto == "implement"

    def test_load_await_step(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: await-test
            steps:
              - name: wait
                await: approval
                timeout: 86400
        """))
        wf = load_workflow(f)
        assert wf.steps[0].await_event == "approval"

    def test_adhoc_workflow(self):
        wf = Workflow.adhoc("Fix the bug")
        assert wf.name == "adhoc"
        assert len(wf.steps) == 1
        assert wf.steps[0].name == "task"
        assert wf.steps[0].prompt == "Fix the bug"

    def test_step_by_name(self):
        wf = Workflow(name="t", steps=[
            StepDef(name="a", prompt="A"),
            StepDef(name="b", prompt="B"),
        ])
        assert wf.step_by_name("b").prompt == "B"
        assert wf.step_by_name("x") is None

    def test_step_index(self):
        wf = Workflow(name="t", steps=[
            StepDef(name="a"), StepDef(name="b"), StepDef(name="c"),
        ])
        assert wf.step_index("b") == 1
        assert wf.step_index("z") == -1


# ---------------------------------------------------------------------------
# Handoff validation
# ---------------------------------------------------------------------------

class TestHandoffValidation:
    def test_all_required_present(self):
        step = StepDef(name="t", handoff=HandoffContract(required=["a", "b"]))
        missing = _validate_handoff(step, {"a": 1, "b": 2, "c": 3})
        assert missing == []

    def test_missing_required(self):
        step = StepDef(name="t", handoff=HandoffContract(required=["a", "b"]))
        missing = _validate_handoff(step, {"a": 1})
        assert missing == ["b"]

    def test_no_required(self):
        step = StepDef(name="t", handoff=HandoffContract())
        assert _validate_handoff(step, {}) == []

    def test_empty_handoff(self):
        step = StepDef(name="t", handoff=HandoffContract(required=["x"]))
        assert _validate_handoff(step, {}) == ["x"]


class TestReadHandoff:
    def test_reads_yaml_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.orchestrator.HANDOFF_DIR", tmp_path)
        (tmp_path / "42.md").write_text("---\ncomplexity: medium\nneeds_spec: true\n---\nNotes here")
        result = _read_handoff("42")
        assert result["complexity"] == "medium"
        assert result["needs_spec"] is True

    def test_missing_handoff_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.orchestrator.HANDOFF_DIR", tmp_path)
        assert _read_handoff("999") == {}

    def test_case_insensitive_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.orchestrator.HANDOFF_DIR", tmp_path)
        (tmp_path / "abc.md").write_text("---\nstatus: done\n---\n")
        assert _read_handoff("ABC")["status"] == "done"


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

class TestBuildStepPrompt:
    def test_includes_handoff_contract(self):
        step = StepDef(name="t", prompt="Do work",
                       handoff=HandoffContract(required=["a"], optional=["b"]))
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx)
        assert "Do work" in prompt
        assert "`a` (required)" in prompt
        assert "`b` (optional)" in prompt

    def test_no_contract_when_empty(self):
        step = StepDef(name="t", prompt="Just do it")
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx)
        assert "handoff" not in prompt.lower()


# ---------------------------------------------------------------------------
# Orchestrator step sequencing & events
# ---------------------------------------------------------------------------

class TestRunWorkflow:
    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_single_step_completes(self, mock_reg, mock_emit, mock_run):
        mock_reg.return_value = MagicMock()
        mock_run.return_value = MagicMock(success=True)

        wf = Workflow.adhoc("Say hello")
        result = run_workflow(wf, task="Say hello", repo="test", cwd="/tmp", issue_id="1")

        assert result is True
        mock_run.assert_called_once()
        event_types = [c[0][0] for c in mock_emit.call_args_list]
        assert "engineer/workflow.started" in event_types
        assert "engineer/step.started" in event_types
        assert "engineer/step.completed" in event_types
        assert "engineer/workflow.completed" in event_types

    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_step_failure_stops_workflow(self, mock_reg, mock_emit, mock_run):
        mock_reg.return_value = MagicMock()
        mock_run.return_value = MagicMock(success=False, error="build failed")

        wf = Workflow(name="t", steps=[
            StepDef(name="build", prompt="build it"),
            StepDef(name="deploy", prompt="deploy it"),
        ])
        result = run_workflow(wf, task="t", repo="r", cwd="/tmp", issue_id="1")

        assert result is False
        assert mock_run.call_count == 1
        event_types = [c[0][0] for c in mock_emit.call_args_list]
        assert "engineer/step.failed" in event_types
        assert "engineer/workflow.failed" in event_types
        assert "engineer/step.completed" not in event_types

    @patch("modastack.workflow.orchestrator._read_handoff")
    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_route_to_spec_when_needs_spec_true(self, mock_reg, mock_emit, mock_run, mock_handoff):
        """needs_spec=true must route to the spec step (the approval-gate path)."""
        mock_reg.return_value = MagicMock()
        mock_run.return_value = MagicMock(success=True)
        mock_handoff.return_value = {"needs_spec": True}

        # Phase names, not prompts — prompts mention "needs_spec" in their
        # handoff contract, which would mask a routing bug. Asserting on the
        # phase= each step ran under is unambiguous.
        wf = Workflow(name="t", steps=[
            StepDef(name="triage", prompt="triage",
                    handoff=HandoffContract(required=["needs_spec"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it"),
        ])
        result = run_workflow(wf, task="t", repo="r", cwd="/tmp", issue_id="1")

        assert result is True
        # spec runs before implement (no gate in this minimal workflow, so it
        # continues afterward — the gate itself is covered by the await test).
        phases = [c[1].get("phase", "") for c in mock_run.call_args_list]
        assert phases == ["triage", "spec", "implement"]

    @patch("modastack.workflow.orchestrator._read_handoff")
    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_route_to_implement_when_needs_spec_false(self, mock_reg, mock_emit, mock_run, mock_handoff):
        """needs_spec=false skips the spec gate and goes straight to implement."""
        mock_reg.return_value = MagicMock()
        mock_run.return_value = MagicMock(success=True)
        mock_handoff.return_value = {"needs_spec": False}

        wf = Workflow(name="t", steps=[
            StepDef(name="triage", prompt="triage",
                    handoff=HandoffContract(required=["needs_spec"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it"),
        ])
        result = run_workflow(wf, task="t", repo="r", cwd="/tmp", issue_id="1")

        assert result is True
        phases = [c[1].get("phase", "") for c in mock_run.call_args_list]
        assert phases == ["triage", "implement"]

    @patch("modastack.workflow.orchestrator._read_handoff")
    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_await_step_is_a_hard_stop(self, mock_reg, mock_emit, mock_run, mock_handoff):
        """The spec gate: after spec, an await step suspends instead of
        running implement. This is what enforces human approval — implement
        must NOT run automatically."""
        registry = MagicMock()
        mock_reg.return_value = registry
        mock_run.return_value = MagicMock(success=True)
        mock_handoff.return_value = {"needs_spec": True, "spec_url": "http://x"}

        wf = Workflow(name="t", steps=[
            StepDef(name="triage", prompt="triage",
                    handoff=HandoffContract(required=["needs_spec"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec",
                    handoff=HandoffContract(required=["spec_url"])),
            StepDef(name="await_approval", await_event="approval"),
            StepDef(name="implement", prompt="build it"),
        ])
        run_workflow(wf, task="t", repo="r", cwd="/tmp", issue_id="1")

        phases = [c[1].get("phase", "") for c in mock_run.call_args_list]
        assert "implement" not in phases
        assert phases == ["triage", "spec"]
        # The run is parked as waiting for the external approval event.
        statuses = [c[1].get("status") for c in registry.update.call_args_list
                    if "status" in c[1]]
        assert "waiting" in statuses

    @patch("modastack.workflow.orchestrator._read_handoff")
    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_handoff_re_prompt(self, mock_reg, mock_emit, mock_run, mock_handoff):
        mock_reg.return_value = MagicMock()
        mock_run.return_value = MagicMock(success=True)
        mock_handoff.side_effect = [
            {},
            {"status": "done"},
        ]

        wf = Workflow(name="t", steps=[
            StepDef(name="build", prompt="build",
                    handoff=HandoffContract(required=["status"])),
        ])
        result = run_workflow(wf, task="t", repo="r", cwd="/tmp", issue_id="1")

        assert result is True
        assert mock_run.call_count == 2

    @patch("modastack.workflow.orchestrator.run_phase_blocking")
    @patch("modastack.workflow.orchestrator._emit_lifecycle_event")
    @patch("modastack.workflow.orchestrator.get_registry")
    def test_registry_updated(self, mock_reg, mock_emit, mock_run):
        registry = MagicMock()
        mock_reg.return_value = registry
        mock_run.return_value = MagicMock(success=True)

        wf = Workflow.adhoc("hello")
        run_workflow(wf, task="hello", repo="r", cwd="/tmp", issue_id="1")

        registry.register.assert_called_once()
        registry.update.assert_called()
        final_status = registry.update.call_args[1].get("status", "")
        assert final_status == "done"
