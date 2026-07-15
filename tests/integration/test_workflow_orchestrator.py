"""Integration tests for workflow orchestrator.

Exercises schema loading, state persistence, variable resolution,
route steps, and await/resume — all against a real filesystem with
real YAML files, but without a live Claude session.
"""

import json
import shutil
import textwrap
import time

import pytest
import yaml


class TestWorkflowSchemaLoading:
    """Load workflow YAML into Workflow/StepDef dataclasses."""

    def test_load_simple_workflow(self, bobi_env):
        """adhoc.yaml from the fixture loads correctly."""
        from bobi.workflow.schema import load_workflow
        wf = load_workflow(bobi_env.workflows_dir / "adhoc.yaml")

        assert wf.name == "adhoc"
        assert len(wf.steps) == 1
        assert wf.steps[0].name == "task"

    def test_load_multi_step(self, bobi_env):
        """two-step.yaml from the fixture has two steps with timeouts."""
        from bobi.workflow.schema import load_workflow
        wf = load_workflow(bobi_env.workflows_dir / "two-step.yaml")

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

        from bobi.workflow.schema import load_workflow
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

        from bobi.workflow.schema import load_workflow
        wf = load_workflow(wf_path)

        await_step = wf.step_by_name("wait-review")
        assert await_step.await_event == "pr.reviewed"


class TestVariableResolution:
    """VariableContext resolves ${{scope.key}} and conditions."""

    def test_scope_resolution(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "medium", "needs_spec": "true"})

        assert ctx.resolve("complexity=${{triage.complexity}}") == "complexity=medium"

    def test_pipe_filter(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("input", {"name": "MyProject"})

        assert ctx.resolve("${{input.name | lower}}") == "myproject"
        assert ctx.resolve("${{input.name | upper}}") == "MYPROJECT"

    def test_condition_evaluation(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "large"})
        ctx.set_flat("complexity", "large")

        assert ctx.evaluate_condition("complexity == 'large'") is True
        assert ctx.evaluate_condition("complexity == 'small'") is False

    def test_boolean_operators(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("a", "true")
        ctx.set_flat("b", "false")

        assert ctx.evaluate_condition("a and not b") is True
        assert ctx.evaluate_condition("a and b") is False
        assert ctx.evaluate_condition("a or b") is True


class TestWorkflowRunState:
    """WorkflowRun state persistence and queries."""

    def test_create_and_save(self, bobi_env):
        from bobi.workflow.state import WorkflowRun

        run = WorkflowRun.create("test-wf", {"type": "test", "data": {"run_key": "X-1"}})
        run.save()

        loaded = WorkflowRun.load(run.run_id)
        assert loaded.workflow_name == "test-wf"
        assert loaded.status == "running"

    def test_find_waiting(self, bobi_env):
        from bobi.workflow.state import WorkflowRun

        run = WorkflowRun.create("await-wf", {"type": "test", "data": {"run_key": "Y-2"}})
        run.status = "waiting"
        run.await_event = "pr.reviewed"
        run.save()

        found = WorkflowRun.find_waiting("pr.reviewed", run_key="Y-2")
        assert found is not None
        assert found.run_id == run.run_id

    def test_find_waiting_no_match(self, bobi_env):
        from bobi.workflow.state import WorkflowRun
        assert WorkflowRun.find_waiting("nonexistent.event") is None

    def test_list_runs(self, bobi_env):
        from bobi.workflow.state import WorkflowRun

        run = WorkflowRun.create("list-wf", {"type": "test", "data": {}})
        run.save()

        runs = WorkflowRun.list_runs()
        assert any(r.run_id == run.run_id for r in runs)

    def test_claim_atomicity(self, bobi_env):
        """Only one caller can claim a run for resume."""
        from bobi.workflow.state import WorkflowRun

        run = WorkflowRun.create("claim-wf", {"type": "test", "data": {}})
        run.status = "waiting"
        run.save()

        assert run.claim() is True
        # Second claim should fail (file renamed)
        run2 = WorkflowRun.create("claim-wf", {"type": "test", "data": {}})
        run2.run_id = run.run_id
        assert run2.claim() is False


class TestNotifyAwaitDeliveryGuard:
    """Integration coverage for notify -> await last-mile delivery."""

    def test_undeliverable_notify_before_await_fails_instead_of_waiting(
        self, stub_bobi_env,
    ):
        from bobi.runtime_guard import with_mutable_runtime_package
        from bobi.sdk import SessionRegistry
        from bobi.workflow.orchestrator import make_session_name, run_workflow
        from bobi.workflow.schema import StepDef, Workflow
        from bobi.workflow.state import WorkflowRun

        run_key = "787-missing-channel"
        session_name = make_session_name("notify-await", "test-repo", run_key)
        registry = SessionRegistry()
        session_dir = registry.session_dir(session_name)
        if session_dir.exists():
            shutil.rmtree(session_dir)

        agent_yaml = stub_bobi_env.package_dir / "agent.yaml"
        original_config = agent_yaml.read_text()
        config = yaml.safe_load(original_config)
        config.setdefault("services", []).append({
            "name": "slack",
            "credentials": {"bot_token": "xoxb-test"},
        })
        with with_mutable_runtime_package(stub_bobi_env.project_path):
            agent_yaml.write_text(yaml.dump(config))

        try:
            workflow = Workflow(name="notify-await", steps=[
                StepDef(
                    name="notify_checkin",
                    notify="slack",
                    message="Please approve run #${{input.run_key}}",
                ),
                StepDef(name="await_approval", await_event="approval.received"),
            ])

            result = run_workflow(
                workflow,
                task="Run that cannot resolve a Slack channel",
                repo="test-repo",
                cwd=str(stub_bobi_env.project_path),
                run_key=run_key,
                timeout=30,
                interactive=False,
            )
        finally:
            with with_mutable_runtime_package(stub_bobi_env.project_path):
                agent_yaml.write_text(original_config)

        assert result is False
        assert (
            WorkflowRun.find_waiting(
                "approval.received", run_key=run_key, repo="test-repo",
            )
            is None
        )
        state = json.loads((session_dir / "state.json").read_text())
        assert state["status"] == "failed"
        assert state["phase"] != "await_approval"
        assert "notify_checkin" in state["error"]


class TestWorkflowStepHelpers:
    """Workflow navigation helpers."""

    def test_step_by_name(self):
        from bobi.workflow.schema import Workflow, StepDef
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
        from bobi.workflow.schema import Workflow, StepDef
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
