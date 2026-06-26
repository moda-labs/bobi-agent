"""Unit tests for the workflow orchestrator — schema parsing, handoff
validation, route evaluation, step sequencing, and event emission."""

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
from dataclasses import dataclass

import pytest

from bobi.workflow.schema import (
    Workflow, StepDef, HandoffContract, load_workflow,
)
from bobi.workflow.orchestrator import (
    _build_step_prompt, _read_handoff, _validate_handoff,
    _setup_worktree,
    run_workflow, resume_workflow, try_resume_for_event,
    make_session_name,
)
from bobi.workflow.state import WorkflowRun


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

class TestSchemaLoad:
    def test_load_simple_workflow(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: test-wf
            trigger: >
              When an issue is assigned and requires code changes.
            steps:
              - name: greet
                prompt: "Say hello"
                handoff:
                  required: [greeting]
                timeout: 60
        """))
        wf = load_workflow(f)
        assert wf.name == "test-wf"
        assert "issue is assigned" in wf.trigger
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
        assert make_session_name("adhoc", "bobi", "99") == \
            "wf-adhoc-bobi-99"


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
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"
        session_dir = tmp_path / "wf-test-42"
        session_dir.mkdir()
        (session_dir / "handoff-setup.yaml").write_text("complexity: medium\nneeds_spec: true\n")
        result = _read_handoff("wf-test-42", "setup")
        assert result["complexity"] == "medium"
        assert result["needs_spec"] is True

    def test_missing_handoff_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"
        assert _read_handoff("wf-test-999", "setup") == {}

    def test_step_specific_handoffs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"
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
    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        """Prompt building reads handoffs via the session registry, which
        needs a bound root — bind explicitly, don't rely on leakage from
        earlier tests."""
        monkeypatch.setattr("bobi.paths._root", tmp_path)

    def test_includes_handoff_contract(self):
        step = StepDef(name="setup", prompt="Do work",
                       handoff=HandoffContract(required=["a"], optional=["b"]))
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx, session_name="wf-test-42", step_name="setup")
        assert "Do work" in prompt
        assert "a: <value>" in prompt
        assert "b: <value>" in prompt
        assert "handoff-setup.yaml" in prompt

    def test_no_contract_when_empty(self):
        step = StepDef(name="t", prompt="Just do it")
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        prompt = _build_step_prompt(step, ctx)
        assert "handoff" not in prompt.lower()


# ---------------------------------------------------------------------------
# Route condition evaluation with flat variables
# ---------------------------------------------------------------------------

class TestRouteConditions:
    def test_flat_variable_resolves_in_condition(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "true")
        assert ctx.evaluate_condition("needs_spec == true") is True

    def test_flat_variable_false(self):
        from bobi.workflow.variables import VariableContext
        ctx = VariableContext()
        ctx.set_flat("needs_spec", "false")
        assert ctx.evaluate_condition("needs_spec == true") is False

    def test_scoped_variable_still_works(self):
        from bobi.workflow.variables import VariableContext
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
    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path)

    def _mock_asyncio_run(self, workflow, **kwargs):
        """Run the workflow with a mocked SDK client."""
        cwd = kwargs.get("cwd", "/tmp")
        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
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
        result = self._mock_asyncio_run(wf, task="Say hello", repo="test", cwd="/tmp", run_key="1")
        assert result is True

    def test_multi_step_completes(self):
        wf = Workflow(name="t", steps=[
            StepDef(name="setup", prompt="set up"),
            StepDef(name="build", prompt="build it"),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", run_key="1")
        assert result is True

    def test_route_step_branches(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"

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
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", run_key="1")
        assert result is True

    def test_session_name_is_deterministic(self):
        name1 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        name2 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        assert name1 == name2 == "wf-issue-lifecycle-jobtack-42"

    def test_different_issues_different_names(self):
        name1 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "42")
        name2 = make_session_name("issue-lifecycle", "moda-labs/jobtack", "43")
        assert name1 != name2


class FailingClient:
    """ClaudeSDKClient mock whose turn yields no ResultMessage — _drain_response
    returns None, driving the orchestrator's failure path."""

    def __init__(self):
        self.connected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, text):
        pass

    async def receive_response(self):
        yield FakeAssistantMessage(content=[FakeTextBlock(text="...")])
        # no ResultMessage → drain returns None → step fails

    async def disconnect(self):
        self.connected = False


class TestHonestTerminalEmit:
    """MDS-65 RC#2/RC#4 — the orchestrator must emit the HONEST terminal session
    event (session.failed on failure, never session.completed after a failure)
    and carry requested_by so the launcher can route it to the requester."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path)

    def _run_capture(self, workflow, client_cls, **kwargs):
        cwd = kwargs.get("cwd", "/tmp")
        emits = []
        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event",
                   side_effect=lambda etype, data, **kw: emits.append((etype, data))), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: client_cls(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            result = run_workflow(workflow, **kwargs)
        return result, emits

    def test_failure_emits_session_failed_not_completed(self):
        wf = Workflow(name="t", steps=[StepDef(name="task", prompt="do it")])
        result, emits = self._run_capture(
            wf, FailingClient, task="t", repo="r", cwd="/tmp", run_key="1",
            requested_by={"slack_user": "U1", "thread_ts": "123.45"},
        )
        assert result is False
        types = [e[0] for e in emits]
        assert "agent/session.failed" in types
        # The bug: session.completed must NOT be emitted on a failure.
        assert "agent/session.completed" not in types
        # RC#4: the failure event carries requested_by for routing.
        failed = next(d for t, d in emits if t == "agent/session.failed")
        assert failed["requested_by"] == {"slack_user": "U1", "thread_ts": "123.45"}

    def test_success_emits_session_completed_with_requested_by(self):
        wf = Workflow(name="t", steps=[StepDef(name="task", prompt="do it")])
        result, emits = self._run_capture(
            wf, FakeClient, task="t", repo="r", cwd="/tmp", run_key="1",
            requested_by={"slack_user": "U2"},
        )
        assert result is True
        types = [e[0] for e in emits]
        assert "agent/session.completed" in types
        assert "agent/session.failed" not in types
        done = next(d for t, d in emits if t == "agent/session.completed")
        assert done["requested_by"] == {"slack_user": "U2"}

    def test_suspend_does_not_emit_terminal_session_event(self, tmp_path, monkeypatch):
        """An await/suspend is dormant, not terminal: it must emit
        workflow.suspended but NEITHER session.completed NOR session.failed —
        else the (now-subscribed) manager is told the agent finished while it
        waits for the external event."""
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_r")
        (tmp_path / "_r" / ".bobi" / "state" / "workflow" / "runs").mkdir(
            parents=True, exist_ok=True)
        (tmp_path / "_r" / ".bobi" / "sessions").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_r")

        wf = Workflow(name="t", steps=[StepDef(name="wait", await_event="approval")])
        result, emits = self._run_capture(
            wf, FakeClient, task="t", repo="r", cwd="/tmp", run_key="1",
            requested_by={"slack_user": "U3"},
        )
        types = [e[0] for e in emits]
        assert "agent/workflow.suspended" in types
        assert "agent/session.completed" not in types
        assert "agent/session.failed" not in types


# ---------------------------------------------------------------------------
# Await / resume
# ---------------------------------------------------------------------------

class TestAwaitStep:
    def _mock_asyncio_run(self, workflow, **kwargs):
        cwd = kwargs.get("cwd", "/tmp")
        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            return run_workflow(workflow, **kwargs)

    def test_await_suspends_workflow(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "state" / "workflow" / "runs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True, exist_ok=True)

        wf = Workflow(name="t", steps=[
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="wait", await_event="approval"),
            StepDef(name="implement", prompt="build it"),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", run_key="1")
        assert result is True

        run = WorkflowRun.find_waiting("approval")
        assert run is not None
        assert run.status == "waiting"
        assert run.await_event == "approval"
        assert run.suspended_at_step == 2
        assert run.session_name == "wf-t-r-1"
        assert run.run_key == "1"

    def test_resume_continues_from_suspended_step(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True)
        tmp_path = tmp_path / "_repo" / ".bobi" / "sessions"
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "state" / "workflow" / "runs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True, exist_ok=True)

        run = WorkflowRun.create("t", {"data": {"run_key": "1"}})
        run.status = "waiting"
        run.suspended_at_step = 1
        run.await_event = "approval"
        run.session_name = "wf-t-r-1"
        run.variable_scopes = {"input": {"task": "t", "repo": "r", "run_key": "1"}}
        run.repo = "r"
        run.cwd = "/tmp"
        run.run_key = "1"
        run.save()

        wf = Workflow(name="t", steps=[
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it"),
        ])

        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            success = resume_workflow(run, wf)

        assert success is True
        reloaded = WorkflowRun.load(run.run_id)
        assert reloaded.status == "completed"

    def test_find_waiting_returns_none_when_no_match(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "state" / "workflow" / "runs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True, exist_ok=True)
        assert WorkflowRun.find_waiting("approval") is None

    def test_find_waiting_filters_by_run_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        (tmp_path / "_repo" / ".bobi" / "state" / "workflow" / "runs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_repo" / ".bobi" / "sessions").mkdir(parents=True, exist_ok=True)

        run = WorkflowRun.create("t", {"data": {"run_key": "42"}})
        run.status = "waiting"
        run.await_event = "approval"
        run.save()

        assert WorkflowRun.find_waiting("approval", run_key="42") is not None
        assert WorkflowRun.find_waiting("approval", run_key="99") is None


# ---------------------------------------------------------------------------
# QA phase in issue-lifecycle
# ---------------------------------------------------------------------------

class TestQAPhase:
    """Tests for the QA phase added after the PR step."""

    def test_issue_lifecycle_has_qa_step(self):
        wf_path = Path(__file__).parent.parent / ".bobi" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not in worktree")
        wf = load_workflow(wf_path)

        qa_step = wf.step_by_name("qa")
        assert qa_step is not None, "qa step must exist"
        assert qa_step.handoff.required == ["qa_status"]
        assert "qa_findings" in qa_step.handoff.optional

    def test_pickup_step_has_frontend_optional(self):
        wf_path = Path(__file__).parent.parent / ".bobi" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not in worktree")
        wf = load_workflow(wf_path)

        pickup = wf.step_by_name("pickup")
        assert pickup is not None
        assert "has_frontend" in pickup.handoff.optional

    def test_qa_step_runs_after_pr(self):
        wf_path = Path(__file__).parent.parent / ".bobi" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not in worktree")
        wf = load_workflow(wf_path)

        pr_idx = wf.step_index("pr")
        qa_idx = wf.step_index("qa")
        assert qa_idx > pr_idx, "qa must come after pr"

    def test_qa_workflow_with_frontend(self, tmp_path, monkeypatch):
        """Full workflow: frontend project runs QA step."""
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        sessions = tmp_path / "_repo" / ".bobi" / "sessions"
        sessions.mkdir(parents=True)

        original_init = FakeClient.__init__

        def _patched_init(self_client):
            original_init(self_client)
            d = sessions / "wf-t-r-1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "handoff-pickup.yaml").write_text(
                "complexity: medium\nneeds_spec: false\nhas_frontend: true\n"
            )
            (d / "handoff-implement.yaml").write_text("status: done\n")
            (d / "handoff-pr.yaml").write_text("pr_url: https://github.com/test/pr/1\n")
            (d / "handoff-qa.yaml").write_text("qa_status: pass\n")

        monkeypatch.setattr(FakeClient, "__init__", _patched_init)

        wf = Workflow(name="t", steps=[
            StepDef(name="pickup", prompt="triage",
                    handoff=HandoffContract(required=["complexity", "needs_spec"],
                                            optional=["has_frontend"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it",
                    handoff=HandoffContract(required=["status"])),
            StepDef(name="pr", prompt="open PR",
                    handoff=HandoffContract(required=["pr_url"])),
            StepDef(name="qa", prompt="run QA",
                    handoff=HandoffContract(required=["qa_status"],
                                            optional=["qa_findings"])),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", run_key="1")
        assert result is True

    def test_qa_step_skipped_by_agent_for_backend(self, tmp_path, monkeypatch):
        """Backend project: QA step still runs but agent reports not_applicable."""
        monkeypatch.setattr("bobi.paths._root", tmp_path / "_repo")
        sessions = tmp_path / "_repo" / ".bobi" / "sessions"
        sessions.mkdir(parents=True)

        original_init = FakeClient.__init__

        def _patched_init(self_client):
            original_init(self_client)
            d = sessions / "wf-t-r-1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "handoff-pickup.yaml").write_text(
                "complexity: small\nneeds_spec: false\nhas_frontend: false\n"
            )
            (d / "handoff-implement.yaml").write_text("status: done\n")
            (d / "handoff-pr.yaml").write_text("pr_url: https://github.com/test/pr/2\n")
            (d / "handoff-qa.yaml").write_text("qa_status: not_applicable\n")

        monkeypatch.setattr(FakeClient, "__init__", _patched_init)

        wf = Workflow(name="t", steps=[
            StepDef(name="pickup", prompt="triage",
                    handoff=HandoffContract(required=["complexity", "needs_spec"],
                                            optional=["has_frontend"])),
            StepDef(name="route", condition="needs_spec == true",
                    goto="spec", else_goto="implement"),
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it",
                    handoff=HandoffContract(required=["status"])),
            StepDef(name="pr", prompt="open PR",
                    handoff=HandoffContract(required=["pr_url"])),
            StepDef(name="qa", prompt="run QA",
                    handoff=HandoffContract(required=["qa_status"],
                                            optional=["qa_findings"])),
        ])
        result = self._mock_asyncio_run(wf, task="t", repo="r", cwd="/tmp", run_key="1")
        assert result is True

    def _mock_asyncio_run(self, workflow, **kwargs):
        cwd = kwargs.get("cwd", "/tmp")
        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            return run_workflow(workflow, **kwargs)


# ---------------------------------------------------------------------------
# try_resume_for_event
# ---------------------------------------------------------------------------

class TestTryResumeForEvent:
    def test_returns_false_when_no_waiting_run(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr("bobi.workflow.state._runs_dir", lambda: runs_dir)
        assert try_resume_for_event("approval") is False

    def test_returns_false_when_workflow_not_found(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.workflow.state._runs_dir", lambda: runs_dir)

        run = WorkflowRun.create("nonexistent-wf", {"data": {"run_key": "1"}})
        run.status = "waiting"
        run.await_event = "approval"
        run.run_key = "1"
        run.save()

        with patch("bobi.workflow.triggers.WorkflowDispatcher") as mock_cls:
            dispatcher = MagicMock()
            dispatcher.find_workflow.return_value = None
            mock_cls.return_value = dispatcher
            result = try_resume_for_event("approval", "1")

        assert result is False

    def test_resumes_waiting_workflow(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.workflow.state._runs_dir", lambda: runs_dir)

        run = WorkflowRun.create("test-wf", {"data": {"run_key": "5"}})
        run.status = "waiting"
        run.await_event = "approval"
        run.run_key = "5"
        run.session_name = "wf-test-wf-r-5"
        run.save()

        fake_wf = Workflow(name="test-wf", steps=[
            StepDef(name="impl", prompt="build it"),
        ])

        with patch("bobi.workflow.triggers.WorkflowDispatcher") as mock_cls, \
             patch("bobi.workflow.orchestrator.resume_workflow") as mock_resume:
            dispatcher = MagicMock()
            dispatcher.find_workflow.return_value = fake_wf
            mock_cls.return_value = dispatcher
            result = try_resume_for_event("approval", "5", event={"data": {"approved": True}})

        assert result is True

    def test_filters_by_run_key(self, tmp_path, monkeypatch):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.workflow.state._runs_dir", lambda: runs_dir)

        run = WorkflowRun.create("test-wf", {"data": {"run_key": "10"}})
        run.status = "waiting"
        run.await_event = "approval"
        run.run_key = "10"
        run.save()

        assert try_resume_for_event("approval", "999") is False


# ---------------------------------------------------------------------------
# resume_workflow started_at tracking
# ---------------------------------------------------------------------------

class TestResumeWorkflowTimestamps:
    def test_resume_sets_started_at_on_run(self, tmp_path, monkeypatch):
        (tmp_path / ".bobi" / "sessions").mkdir(parents=True)
        monkeypatch.setattr("bobi.paths._root", tmp_path)
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.workflow.state._runs_dir", lambda: runs_dir)

        run = WorkflowRun.create("t", {"data": {"run_key": "1"}})
        run.status = "waiting"
        run.suspended_at_step = 1
        run.await_event = "approval"
        run.session_name = "wf-t-r-1"
        run.variable_scopes = {"input": {"task": "t", "repo": "r", "run_key": "1"}}
        run.repo = "r"
        run.cwd = "/tmp"
        run.run_key = "1"
        run.started_at = "2026-01-01T00:00:00"
        run.save()

        wf = Workflow(name="t", steps=[
            StepDef(name="spec", prompt="write spec"),
            StepDef(name="implement", prompt="build it"),
        ])

        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value="/tmp"), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            success = resume_workflow(run, wf)

        assert success is True
        reloaded = WorkflowRun.load(run.run_id)
        assert reloaded.resumed_at != ""
        assert reloaded.resumed_at != "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Handoff edge cases
# ---------------------------------------------------------------------------

class TestHandoffEdgeCases:
    def test_corrupted_yaml_returns_empty(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".bobi" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.paths._root", tmp_path)
        session_dir = sessions_dir / "wf-test-corrupt"
        session_dir.mkdir()
        (session_dir / "handoff-setup.yaml").write_text(": : : invalid yaml [[[")
        result = _read_handoff("wf-test-corrupt", "setup")
        assert result == {}

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".bobi" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr("bobi.paths._root", tmp_path)
        session_dir = sessions_dir / "wf-test-empty"
        session_dir.mkdir()
        (session_dir / "handoff-setup.yaml").write_text("")
        result = _read_handoff("wf-test-empty", "setup")
        assert result == {}


# ---------------------------------------------------------------------------
# Worktree setup
# ---------------------------------------------------------------------------

class TestSetupWorktree:
    def test_worktree_creation(self, tmp_path):
        import subprocess as sp
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "--allow-empty", "-m", "init"],
               cwd=tmp_path, capture_output=True)

        result = _setup_worktree(str(tmp_path), "test-session")
        expected = tmp_path / ".claude" / "worktrees" / "test-session"
        assert result == str(expected)
        assert expected.exists()

    def test_existing_worktree_reused(self, tmp_path):
        import subprocess as sp
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "--allow-empty", "-m", "init"],
               cwd=tmp_path, capture_output=True)

        first = _setup_worktree(str(tmp_path), "reuse-session")
        second = _setup_worktree(str(tmp_path), "reuse-session")
        assert first == second

    def test_worktree_failure_raises_not_fallback(self, tmp_path):
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()

        with pytest.raises(RuntimeError, match="Failed to create worktree"):
            _setup_worktree(str(non_git), "will-fail")
