"""A failed routed turn may rerun on the control model only when no tool ran.

A rerun replays the whole task in a fresh session, so any tool the failed turn
already ran (a push, a post) would run twice. Each test drives a real call
site with a fake brain stream that ran a tool before failing.
"""

import asyncio

import pytest

import bobi.session as session_module
from bobi import paths
from bobi.brain.base import AssistantText, BrainToolExecution, TurnResult
from bobi.brain.codex import _CodexSession
from bobi.inbox import Message
from bobi.metrics.routing import may_rerun_on_control
from bobi.workflow.orchestrator import _drain_response
from tests.metrics.helpers import decision


class _LostTurnClient:
    """Streams text after a tool ran, then never delivers a terminal result."""

    provider = "anthropic"

    def __init__(self, end):
        self.end = end

    async def receive_response(self):
        yield AssistantText(text="pushed the branch, writing the summary")
        if self.end == "timeout":
            raise asyncio.TimeoutError()


@pytest.mark.parametrize("end", ["timeout", "network_drop"])
def test_workflow_turn_without_a_terminal_result_blocks_the_rerun(tmp_path, monkeypatch, end):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    paths.bind_root(tmp_path)
    drain = asyncio.run(_drain_response(_LostTurnClient(end), "wf-session", model="cheap"))
    assert drain.final_text is None
    assert drain.tools_ran is True
    assert not may_rerun_on_control(decision(), error_kind=drain.error_kind,
                                    tools_ran=drain.tools_ran)


def _codex_runner(events):
    async def run(argv, cwd, prompt):
        for event in events:
            yield event
    return run


_RAN_A_PUSH = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "t1", "type": "command_execution"}},
    {"type": "item.completed", "item": {"id": "t1", "type": "command_execution",
                                        "status": "completed", "command": "git push"}},
]


@pytest.mark.parametrize("tail", [
    [{"type": "turn.failed", "error": {"message": "stream disconnected"}}],
    [],
], ids=["turn_failed", "exited_without_completing"])
def test_codex_error_results_report_the_tools_that_ran(tail):
    async def run():
        codex = _CodexSession(cwd=".", instructions="", runner=_codex_runner(_RAN_A_PUSH + tail))
        await codex.connect()
        await codex.query("push the branch")
        return [message async for message in codex.receive_response()][-1]

    result = asyncio.run(run())
    assert result.is_error
    assert [tool.provider_tool_call_id for tool in result.tool_executions] == ["t1"]
    assert not may_rerun_on_control(decision(), error_kind=result.error_kind,
                                    tools_ran=bool(result.tool_executions))


_OVERLOADED = {"is_error": True, "api_error_status": 529,
               "error_message": "Overloaded", "result_text": "529 overloaded"}


class _ScriptedClient:
    provider = "anthropic"

    def __init__(self, results):
        self.results = list(results)
        self.queries = 0

    async def query(self, text):
        self.queries += 1

    async def receive_response(self):
        yield self.results.pop(0)

    async def connect(self):
        pass

    async def disconnect(self):
        pass


def _session(tmp_path, monkeypatch, script):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    paths.bind_root(tmp_path)
    monkeypatch.setattr(session_module, "TURN_RETRY_BASE", 0)
    session = session_module.Session("rerun-guard", str(tmp_path), extra_options={"model": "cheap"})
    session._client = _ScriptedClient(script)
    session._state = "waiting_input"
    session._route_decision = decision()
    reruns = []
    session._route_to_control = lambda error: reruns.append(error) or True
    session._make_brain_session = lambda resume=None: _ScriptedClient(
        [TurnResult(session_id="control", result_text="ok")])
    return session, reruns


def _process(session, text):
    async def run():
        session._input_ready = asyncio.Event()
        await session._process_message(Message(id=text, sender="user", text=text))
    asyncio.run(run())


def test_session_remembers_a_tool_from_an_earlier_retry_attempt(tmp_path, monkeypatch):
    ran_bash = BrainToolExecution(tool_name="Bash", provider_tool_call_id="t1")
    monkeypatch.setattr(session_module, "TURN_RETRY_MAX_ATTEMPTS", 2)
    session, reruns = _session(tmp_path, monkeypatch, [
        TurnResult(session_id="s", tool_executions=[ran_bash], **_OVERLOADED),
        TurnResult(session_id="s", **_OVERLOADED),
        TurnResult(session_id="s", **_OVERLOADED),
    ])
    _process(session, "push the branch")
    # All three attempts ran and failed; the tool from the first one blocks the rerun.
    assert session._client.queries == 3
    assert session._state == "waiting_input"
    assert reruns == []


def test_session_starts_each_message_with_no_tools_ran(tmp_path, monkeypatch):
    session, reruns = _session(tmp_path, monkeypatch, [
        TurnResult(session_id="s", is_error=True, error_message="bad request",
                   result_text="failed"),
    ])
    session._last_tools_ran = True  # left over from an earlier message
    _process(session, "answer a question")
    assert len(reruns) == 1


@pytest.mark.parametrize("resume_connects", [True, False], ids=["resumed", "resume_failed"])
def test_workflow_remembers_tools_from_before_a_turn_cap_resume(
    bobi_install, monkeypatch, resume_connects,
):
    from bobi.brain.base import ERROR_KIND_MAX_TURNS
    from bobi.brain.stub import StubBrain
    from bobi.metrics.routing import RouteOutcome
    from bobi.workflow import orchestrator
    from bobi.workflow.orchestrator import DrainResult, run_workflow
    from bobi.workflow.schema import StepDef, Workflow

    brain = StubBrain()
    make_session = brain.make_session

    def session_for(**kwargs):
        client = make_session(**kwargs)
        if kwargs.get("resume") and not resume_connects:
            async def refuse():
                raise RuntimeError("test-only resume failure")
            client.connect = refuse
        return client

    monkeypatch.setattr(brain, "make_session", session_for)
    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    monkeypatch.setattr("bobi.brain.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.session.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.events.publish.post_event", lambda *args, **kwargs: True)

    async def routed(ctx, **kwargs):
        return RouteOutcome("cheap", decision())

    monkeypatch.setattr("bobi.metrics.routing.resolve_route", routed)
    reruns = []
    monkeypatch.setattr("bobi.metrics.routing.candidate_failed",
                        lambda *args, **kwargs: reruns.append(kwargs) or None)
    monkeypatch.setattr(orchestrator, "load_session_id", lambda name: "saved-session")
    drains = iter([
        DrainResult(None, "turn cap", ERROR_KIND_MAX_TURNS, tools_ran=True),
        DrainResult(None, "overloaded", "", tools_ran=False),
    ])
    drain_calls = []

    async def scripted_drain(*args, **kwargs):
        drain_calls.append(1)
        return next(drains)

    monkeypatch.setattr(orchestrator, "_drain_response", scripted_drain)
    run_workflow(Workflow(name="routed", steps=[
        StepDef(name="work", prompt="do the work", agent="engineer")]),
        task="test", repo="test", cwd=str(bobi_install.repo_path), run_key="resume-guard")
    assert reruns == []
    assert len(drain_calls) == (2 if resume_connects else 1)
