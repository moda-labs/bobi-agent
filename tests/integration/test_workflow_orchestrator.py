"""Integration tests for workflow orchestrator.

Exercises schema loading, state persistence, variable resolution,
route steps, and await/resume — all against a real filesystem with
real YAML files, but without a live Claude session.
"""

import json
import textwrap
import time

import pytest
import yaml


class TestWorkflowSchemaLoading:
    """Load workflow YAML into Workflow/StepDef dataclasses."""

    def test_load_simple_workflow(self, modastack_env):
        """adhoc.yaml from the fixture loads correctly."""
        from modastack.workflow.schema import load_workflow
        wf = load_workflow(modastack_env.workflows_dir / "adhoc.yaml")

        assert wf.name == "adhoc"
        assert len(wf.steps) == 1
        assert wf.steps[0].name == "task"

    def test_load_multi_step(self, modastack_env):
        """two-step.yaml from the fixture has two steps with timeouts."""
        from modastack.workflow.schema import load_workflow
        wf = load_workflow(modastack_env.workflows_dir / "two-step.yaml")

        assert wf.name == "two-step"
        assert len(wf.steps) == 2
        assert wf.steps[0].timeout == 60
        assert wf.steps[1].timeout == 60

    def test_load_route_workflow(self, tmp_path):
        """Route step with if/goto/else parses correctly."""
        wf_path = tmp_path / "route.yaml"
        wf_path.write_text(textwrap.dedent("""\
            name: with-route
            steps:
              - name: triage
                prompt: "Classify the issue"
                handoff:
                  required: [complexity]
              - name: route
                if: "complexity == 'large'"
                goto: spec
                else: implement
              - name: spec
                prompt: "Write a spec"
              - name: implement
                prompt: "Build it"
        """))

        from modastack.workflow.schema import load_workflow
        wf = load_workflow(wf_path)

        route = wf.step_by_name("route")
        assert route is not None
        assert route.condition == "complexity == 'large'"
        assert route.goto == "spec"
        assert route.else_goto == "implement"

    def test_load_await_workflow(self, tmp_path):
        """Await step parses the await field."""
        wf_path = tmp_path / "with-await.yaml"
        wf_path.write_text(textwrap.dedent("""\
            name: with-await
            steps:
              - name: build
                prompt: "Build it"
              - name: wait-review
                await: pr.reviewed
              - name: merge
                prompt: "Merge it"
        """))

        from modastack.workflow.schema import load_workflow
        wf = load_workflow(wf_path)

        await_step = wf.step_by_name("wait-review")
        assert await_step.await_event == "pr.reviewed"


class TestVariableResolution:
    """VariableContext resolves ${{scope.key}} and conditions."""

    def test_scope_resolution(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "medium", "needs_spec": "true"})

        assert ctx.resolve("complexity=${{triage.complexity}}") == "complexity=medium"

    def test_pipe_filter(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("input", {"name": "MyProject"})

        assert ctx.resolve("${{input.name | lower}}") == "myproject"
        assert ctx.resolve("${{input.name | upper}}") == "MYPROJECT"

    def test_condition_evaluation(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "large"})
        ctx.set_flat("complexity", "large")

        assert ctx.evaluate_condition("complexity == 'large'") is True
        assert ctx.evaluate_condition("complexity == 'small'") is False

    def test_boolean_operators(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("a", "true")
        ctx.set_flat("b", "false")

        assert ctx.evaluate_condition("a and not b") is True
        assert ctx.evaluate_condition("a and b") is False
        assert ctx.evaluate_condition("a or b") is True


class TestWorkflowRunState:
    """WorkflowRun state persistence and queries."""

    def test_create_and_save(self, modastack_env):
        from modastack.workflow.state import WorkflowRun

        run = WorkflowRun.create("test-wf", {"type": "test", "data": {"run_key": "X-1"}})
        run.save()

        loaded = WorkflowRun.load(run.run_id)
        assert loaded.workflow_name == "test-wf"
        assert loaded.status == "running"

    def test_find_waiting(self, modastack_env):
        from modastack.workflow.state import WorkflowRun

        run = WorkflowRun.create("await-wf", {"type": "test", "data": {"run_key": "Y-2"}})
        run.status = "waiting"
        run.await_event = "pr.reviewed"
        run.save()

        found = WorkflowRun.find_waiting("pr.reviewed", run_key="Y-2")
        assert found is not None
        assert found.run_id == run.run_id

    def test_find_waiting_no_match(self, modastack_env):
        from modastack.workflow.state import WorkflowRun
        assert WorkflowRun.find_waiting("nonexistent.event") is None

    def test_list_runs(self, modastack_env):
        from modastack.workflow.state import WorkflowRun

        run = WorkflowRun.create("list-wf", {"type": "test", "data": {}})
        run.save()

        runs = WorkflowRun.list_runs()
        assert any(r.run_id == run.run_id for r in runs)

    def test_claim_atomicity(self, modastack_env):
        """Only one caller can claim a run for resume."""
        from modastack.workflow.state import WorkflowRun

        run = WorkflowRun.create("claim-wf", {"type": "test", "data": {}})
        run.status = "waiting"
        run.save()

        assert run.claim() is True
        # Second claim should fail (file renamed)
        run2 = WorkflowRun.create("claim-wf", {"type": "test", "data": {}})
        run2.run_id = run.run_id
        assert run2.claim() is False


class TestWorkflowStepHelpers:
    """Workflow navigation helpers."""

    def test_step_by_name(self):
        from modastack.workflow.schema import Workflow, StepDef
        wf = Workflow(
            name="test",
            steps=[
                StepDef(name="a", prompt="do a"),
                StepDef(name="b", prompt="do b"),
            ],
        )
        assert wf.step_by_name("b").prompt == "do b"
        assert wf.step_by_name("c") is None

    def test_step_index(self):
        from modastack.workflow.schema import Workflow, StepDef
        wf = Workflow(
            name="test",
            steps=[
                StepDef(name="a", prompt="do a"),
                StepDef(name="b", prompt="do b"),
            ],
        )
        assert wf.step_index("a") == 0
        assert wf.step_index("b") == 1
        assert wf.step_index("missing") == -1
