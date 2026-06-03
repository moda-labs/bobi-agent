"""Unit tests for the workflow orchestrator — schema parsing, handoff
validation, route evaluation, step sequencing, and event emission."""

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
from dataclasses import dataclass

import pytest

from modastack.workflow.schema import (
    Workflow, StepDef, HandoffContract, load_workflow,
)
from modastack.workflow.orchestrator import (
    _build_step_prompt, _read_handoff, _validate_handoff,
    run_workflow, make_session_name,
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

    def test_workflow_with_description(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: test-wf
            description: A test workflow for unit tests.
            steps:
              - name: work
                prompt: "Do the thing"
        """))
        wf = load_workflow(f)
        assert wf.description == "A test workflow for unit tests."

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


class TestSessionName:
    def test_deterministic(self):
        assert make_session_name("issue-lifecycle", "moda-labs/jobtack", "42") == \
            "wf-issue-lifecycle-jobtack-42"

    def test_plain_repo(self):
        assert make_session_name("adhoc", "modastack", "99") == \
            "wf-adhoc-modastack-99"


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
    def test_reads_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
        session_dir = tmp_path / "wf-test-42"
        session_dir.mkdir()
        (session_dir / "handoff-setup.yaml").write_text("complexity: medium\nneeds_spec: true\n")
        result = _read_handoff("wf-test-42", "setup")
        assert result["complexity"] == "medium"
        assert result["needs_spec"] is True

    def test_missing_handoff_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
        assert _read_handoff("wf-test-999", "setup") == {}

    def test_step_specific_handoffs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
        session_dir = tmp_path / "wf-test-1"
        session_dir.mkdir()
        (session_dir / "handoff-setup.yaml").write_text("worktree: /tmp/wt\n")
        (session_dir / "handoff-pickup.yaml").write_text("complexity: medium\n")
        assert _read_handoff("wf-test-1", "setup")["worktree"] == "/tmp/wt"
        assert _read_handoff("wf-test-1", "pickup")["complexity"] == "medium"


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

class TestBuildStepPrompt:
    def test_includes_handoff_contract(self):
        step = StepDef(name="setup", prompt="Do work",
                       handoff=HandoffContract(required=["a"], optional=["b"]))
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx, session_name="wf-test-42", step_name="setup")
        assert "Do work" in prompt
        assert "a: <value>" in prompt
        assert "b: <value>" in prompt
        assert "handoff-setup.yaml" in prompt

    def test_no_contract_when_empty(self):
        step = StepDef(name="t", prompt="Just do it")
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx)
        assert "handoff" not in prompt.lower()


# ---------------------------------------------------------------------------
# Route condition evaluation with flat variables
# ---------------------------------------------------------------------------

class TestRouteConditions:
    def test_flat_variable_resolves_in_condition(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "true")
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_flat_variable_false(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "false")
        assert ctx.evaluate_condition("needs_spec == true") is False

    def test_scoped_variable_still_works(self):
        from modastack.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "medium"})
        assert ctx.evaluate_condition("${{triage.complexity}} == medium") is True


# ---------------------------------------------------------------------------
# Orchestrator integration (mock the SDK client)
# ---------------------------------------------------------------------------

@dataclass
class FakeResultMessage:
    session_id: str = "test-session-id"
    duration_ms: int = 1000
    total_cost_usd: float = 0.01
    num_turns: int = 1
    is_error: bool = False
    result: str = ""
    deferred_tool_use: object = None


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeAssistantMessage:
    content: list


class FakeClient:
    """Mock ClaudeSDKClient that yields one turn per query."""

    def __init__(self):
        self.queries = []
        self.connected = False

    async def connect(self, prompt=None):
        self.connected = True
        if prompt:
            self.queries.append(prompt)

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        yield FakeAssistantMessage(content=[FakeTextBlock(text="Done.")])
        yield FakeResultMessage()

    async def disconnect(self):
        self.connected = False


class TestRunWorkflow:
    def _mock_asyncio_run(self, workflow, **kwargs):
        """Run the workflow with a mocked SDK client."""
        with patch("modastack.workflow.orchestrator.get_registry") as mock_reg, \
             patch("modastack.workflow.orchestrator._emit_lifecycle_event"), \
             patch("modastack.workflow.orchestrator.load_session_id", return_value=""), \
             patch("modastack.workflow.orchestrator.save_session_id"), \
             patch("modastack.workflow.orchestrator.log_activity"), \
             patch("modastack.workflow.orchestrator.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            return run_workflow(workflow, **kwargs)

    def test_single_step_completes(self):
        wf = Workflow(name="adhoc", steps=[StepDef(name="task", prompt="Say hello")])
        result = self._mock_asyncio_run(wf, task="Say hello", repo="test", cwd="/tmp", issue_id="1")
        assert result is True

    def test_multi_step_completes(self):
        wf = Workflow(name="t", steps=[
            StepDef(name="setup", prompt="set up"),
            StepDef(name="build", prompt="build it"),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", issue_id="1")
        assert result is True

    def test_route_step_branches(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        # Write handoff during the fake agent's response (simulating the
        # agent writing it after the triage step runs, not before)
        original_init = FakeClient.__init__
        def _patched_init(self_client):
            original_init(self_client)
            # Write to the session dir handoff path
            d = tmp_path / "wf-t-r-1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "handoff-triage.yaml").write_text("needs_spec: true\n")
        monkeypatch.setattr(FakeClient, "__init__", _patched_init)

        wf = Workflow(name="t", steps=[
            StepDef(name="triage", prompt="triage",
                    handoff=HandoffContract(required=["needs_spec"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it"),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", issue_id="1")
        assert result is True

    def test_session_name_is_deterministic(self):
        name1 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        name2 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        assert name1 == name2 == "wf-issue-lifecycle-jobtack-42"

    def test_different_issues_different_names(self):
        name1 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        name2 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "43")
        assert name1 != name2
