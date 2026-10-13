"""A gate must not change which model a routed workflow runs.

Each shape runs twice through the real orchestrator and the real metrics
runtime, once straight through and once parked at approval gates and resumed:
every executed step's (step, agent, model), the route admitted at the start,
and the route left in the final checkpoint must match. Shapes whose routing
config changes while the run is parked state where and why they differ, as
docs/JEV_ROUTER_DATA_FLOW.md section 4.2 describes.
"""

import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from bobi import paths
from bobi.workflow.orchestrator import resume_workflow, run_workflow
from bobi.workflow.schema import StepDef, Workflow
from bobi.workflow.state import WorkflowRun
from tests.metrics.helpers import configured, fixed_policy
from tests.test_orchestrator import FakeBrainClient, _bind_runtime_root

E, R = "engineer", "reviewer"
# The engineer has no configured model and runs the process default unless routed.
ENGINEER_MODEL, REVIEWER_MODEL = "opus", "claude-3-7-sonnet"
CONTROL, PICK = "sonnet", "haiku"
GATE = "gate"


def _config(**changes) -> dict:
    return configured(**{"mode": "enforce", "entry_points": ["workflow_start"], "roles": [E],
                         "control_model": CONTROL, "candidate_models": [CONTROL, PICK], **changes})


ROUTED = _config()
EDITED = _config(instructions="Route trivial tasks to haiku.")
OUT_OF_SCOPE = _config(roles=[R])
MODEL_REMOVED = _config(candidate_models=[CONTROL, "gpt-5"])


@dataclass(frozen=True)
class Shape:
    # (name, agent, step-level model) or a route StepDef; GATE marks where the gated run parks.
    steps: tuple
    role: str = ""
    model: str = ""
    config: dict | None = field(default_factory=lambda: ROUTED)
    # The config while parked; the default leaves it unchanged.
    parked_config: dict | None | str = "unchanged"
    saved_session: bool = False
    legacy_record: bool = False
    # The metrics store refuses every route admission (a full spool).
    refuse_admission: bool = False
    # A model the provider cannot start; the policy's pick reruns on control.
    unstartable: str = ""
    # Only this agent's sessions on the unstartable model fail, when set.
    unstartable_agent: str = ""
    reviewer_model: str = REVIEWER_MODEL
    # The run is expected to fail, in both runs alike.
    fails: bool = False
    # Where a config change while parked is documented to differ from the
    # straight run: {step: model} after the gate, and each resume's admissions.
    differs: dict = field(default_factory=dict)
    resume_admissions: tuple | None = None


def _s(name, agent="", model=""):
    return (name, agent, model)


def _route(name, condition, goto, else_goto="", **kwargs):
    return StepDef(name=name, condition=condition, goto=goto, else_goto=else_goto, **kwargs)


IMPLEMENT, REWORK, VERIFY, FIX = _s("implement", E), _s("rework", E), _s("verify", E), _s("fix", E)
REVIEW, REVIEW2 = _s("review", R), _s("review2", R)

SHAPES = {
    "routed-agent-first-others-after-gate": Shape((IMPLEMENT, GATE, REVIEW, REVIEW2, REWORK)),
    "routed-agent-first-others-after-gate-saved-session": Shape(
        (IMPLEMENT, GATE, REVIEW, REVIEW2, REWORK), saved_session=True),
    "record-without-agent": Shape(
        (IMPLEMENT, GATE, REVIEW, REVIEW2, REWORK), saved_session=True, legacy_record=True),
    "routed-agent-before-and-after-gate": Shape((IMPLEMENT, REVIEW, GATE, REWORK, REVIEW2, VERIFY)),
    "routed-agent-before-and-after-gate-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, REWORK, REVIEW2, VERIFY), saved_session=True),
    "resume-on-other-agent-then-routed-agent": Shape(
        (IMPLEMENT, REVIEW, GATE, REVIEW2, REWORK, VERIFY), saved_session=True),
    "gate-first": Shape((GATE, IMPLEMENT, REVIEW, REWORK)),
    "gate-late": Shape((IMPLEMENT, REWORK, REVIEW, GATE, VERIFY)),
    "two-gates": Shape((IMPLEMENT, GATE, REVIEW, GATE, REWORK), saved_session=True),
    "routed-agent-not-first": Shape((REVIEW, GATE, IMPLEMENT, VERIFY)),
    "routed-agent-not-first-saved-session": Shape((REVIEW, IMPLEMENT, GATE, VERIFY), saved_session=True),
    "step-model-on-first-step": Shape(
        (_s("spec", E, "gpt-step"), GATE, REVIEW, IMPLEMENT, VERIFY), saved_session=True),
    "step-model-on-first-step-no-saved-session": Shape(
        (_s("spec", E, "gpt-step"), GATE, REVIEW, IMPLEMENT, VERIFY)),
    "step-model-first-after-gate": Shape(
        (IMPLEMENT, GATE, _s("rework", E, "gpt-step"), VERIFY, REVIEW, FIX)),
    "step-model-first-after-gate-saved-session": Shape(
        (IMPLEMENT, GATE, _s("rework", E, "gpt-step"), VERIFY, REVIEW, FIX), saved_session=True),
    "step-model-before-gate": Shape(
        (IMPLEMENT, _s("tune", E, "gpt-step"), GATE, REWORK, VERIFY), saved_session=True),
    "step-model-on-other-agent-after-gate": Shape(
        (IMPLEMENT, GATE, _s("review", R, "gpt-step"), REWORK), saved_session=True),
    "launch-role": Shape((_s("implement"), GATE, _s("rework"), _s("verify")), role=E),
    "launch-role-over-step-agents": Shape((IMPLEMENT, GATE, REVIEW, REWORK), role=E, saved_session=True),
    "launch-role-out-of-scope": Shape((IMPLEMENT, GATE, REVIEW, REWORK), role=R),
    # A step that names no agent inherits the agent that ran before it, across a gate too.
    "agentless-step-first-after-gate": Shape((IMPLEMENT, GATE, _s("polish"), REVIEW, _s("tidy"))),
    "agentless-step-after-other-agent-across-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, _s("polish"), REWORK), saved_session=True),
    "agentless-step-before-gate": Shape((IMPLEMENT, _s("polish"), GATE, REVIEW, _s("tidy"), REWORK)),
    "agentless-steps-only-after-gate": Shape((IMPLEMENT, GATE, _s("polish"), _s("tidy"))),
    "agentless-first-step": Shape((_s("plan"), GATE, IMPLEMENT, _s("polish"), REVIEW)),
    "agentless-step-launch-role": Shape((IMPLEMENT, REVIEW, GATE, _s("polish"), REWORK), role=E),
    "agentless-step-launch-role-out-of-scope": Shape((IMPLEMENT, GATE, _s("polish"), REVIEW), role=R),
    # Route steps: the step that runs after a jump inherits the agent that ran last.
    "forward-jump-to-agentless-step": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("decide", "true", "polish"), _s("tail", E), _s("polish"))),
    "forward-jump-to-agentless-step-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("decide", "true", "polish"), _s("tail", E), _s("polish")),
        saved_session=True),
    "forward-jump-to-agentless-step-old-checkpoint": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("decide", "true", "polish"), _s("tail", E), _s("polish")),
        legacy_record=True),
    "old-checkpoint-agentless-step-after-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, _s("polish"), REWORK), legacy_record=True),
    "jump-before-gate": Shape(
        (IMPLEMENT, _route("skip", "true", "review"), _s("tidy", E), REVIEW, GATE, _s("polish"), REWORK)),
    "conditional-skip-after-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("skip", "false", "rework", "polish"), REWORK, _s("polish"), FIX)),
    "backward-rework-jump-across-gate": Shape(
        (IMPLEMENT, GATE, REVIEW, _s("polish"),
         _route("again", "true", "implement", max_iterations=1, on_exhausted="done"), _s("done")),
        saved_session=True),
    "backward-rework-jump-after-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, REWORK, _s("polish"), REVIEW2,
         _route("again", "true", "polish", max_iterations=1, on_exhausted="done"), _s("done"))),
    "backward-jump-launch-role": Shape(
        (IMPLEMENT, GATE, REVIEW, _route("again", "true", "implement", max_iterations=1,
                                         on_exhausted="done"), _s("done")), role=E),
    # A run whose route admission was refused at its start runs the control model
    # throughout; each resume records the fallback it runs on.
    "admission-refused": Shape((IMPLEMENT, GATE, REVIEW, REWORK), refuse_admission=True,
                               resume_admissions=((E, None, "admission_rejected"),)),
    "admission-refused-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, REWORK, VERIFY), refuse_admission=True, saved_session=True,
        resume_admissions=((E, None, "admission_rejected"),)),
    "policy-pinned-to-another-brain": Shape(
        (IMPLEMENT, REVIEW, GATE, REWORK), config=_config(brain="claude"), saved_session=True),
    # Transcript continuity across a jump to the step that ran before the gate's transcript.
    "forward-jump-continues-transcript": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("decide", "true", "polish"), _s("tail", E), _s("polish"), FIX),
        saved_session=True),
    "same-agent-continues-transcript": Shape((IMPLEMENT, GATE, REWORK, VERIFY), saved_session=True),
    "other-agent-next-saved-session": Shape((IMPLEMENT, REVIEW, GATE, REWORK, REVIEW2), saved_session=True),
    # The policy's model cannot start: its first step reruns on control, even
    # when a route jumps to another agent's step first.
    # A failure before the gate rewrites the record to control, which a resume re-admits.
    "policy-model-fails-to-start": Shape((IMPLEMENT, REVIEW, GATE, REWORK), unstartable=PICK,
                                         resume_admissions=((E, CONTROL, None),)),
    "policy-model-fails-after-jump-to-other-agent": Shape(
        (_route("d", "true", "review"), IMPLEMENT, REVIEW, REWORK, GATE, FIX), unstartable=PICK,
        resume_admissions=((E, CONTROL, None),)),
    "policy-model-fails-after-jump-across-gate": Shape(
        (_route("d", "true", "review"), IMPLEMENT, REVIEW, GATE, REWORK, VERIFY),
        unstartable=PICK, saved_session=True),
    # Another agent configured to the policy's pick is not the policy's step:
    # its failure is its own, and its success leaves the policy's fallback.
    "other-agent-on-picked-model-fails": Shape(
        (GATE, _route("d", "true", "review"), IMPLEMENT, REVIEW, REWORK),
        reviewer_model=PICK, unstartable=PICK, unstartable_agent=R, fails=True),
    "other-agent-on-picked-model-runs-first-then-policy-model-fails": Shape(
        (_route("d", "true", "review"), IMPLEMENT, REVIEW, REWORK, GATE, FIX),
        reviewer_model=PICK, unstartable=PICK, unstartable_agent=E,
        resume_admissions=((E, CONTROL, None),)),
    "other-agent-on-picked-model-runs-first-policy-model-fails-after-gate": Shape(
        (_route("d", "true", "review"), IMPLEMENT, REVIEW, GATE, REWORK, VERIFY),
        reviewer_model=PICK, unstartable=PICK, unstartable_agent=E, saved_session=True),
    "launch-model": Shape((IMPLEMENT, GATE, REVIEW, REWORK), model="gpt-launch", saved_session=True),
    "no-config": Shape((IMPLEMENT, GATE, REVIEW, REWORK), config=None, saved_session=True),
    # A run that started without a route never gets one on resume.
    "config-added-while-parked": Shape(
        (IMPLEMENT, REVIEW, GATE, REVIEW2, REWORK, VERIFY), config=None, parked_config=ROUTED),
    "config-added-while-parked-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, REVIEW2, REWORK, VERIFY), config=None, parked_config=ROUTED,
        saved_session=True),
    "config-added-then-edited-while-parked": Shape(
        (IMPLEMENT, REVIEW, GATE, REVIEW2, REWORK, VERIFY), config=None, parked_config=EDITED,
        saved_session=True),
    # An edit that keeps the agent in scope and its model a candidate keeps the
    # model the route chose; the resume records that its record is stale.
    "config-edited-while-parked": Shape(
        (IMPLEMENT, GATE, REVIEW, REWORK), parked_config=EDITED,
        resume_admissions=((E, None, "routing_sticky_stale"),)),
    "config-edited-while-parked-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, REVIEW2, REWORK), parked_config=EDITED, saved_session=True,
        resume_admissions=((E, None, "routing_sticky_stale"),)),
    "config-edited-while-parked-step-model-after-gate": Shape(
        (IMPLEMENT, GATE, _s("rework", E, "gpt-step"), VERIFY), parked_config=EDITED, saved_session=True,
        resume_admissions=((E, None, "routing_sticky_stale"),)),
    # Disabling routing returns the route agent to its configured model.
    "config-disabled-while-parked": Shape(
        (IMPLEMENT, GATE, REVIEW, REWORK, VERIFY), parked_config=None, saved_session=True,
        differs={"rework": ENGINEER_MODEL, "verify": ENGINEER_MODEL},
        resume_admissions=((E, None, "routing_disabled_resumed"),)),
    # A routed agent the policy no longer covers runs the control model.
    "agent-out-of-scope-while-parked": Shape(
        (IMPLEMENT, GATE, REVIEW, REWORK), parked_config=OUT_OF_SCOPE, saved_session=True,
        differs={"rework": CONTROL}, resume_admissions=((E, None, "routing_out_of_scope"),)),
    # A routed model taken out of the candidates is never resumed.
    "model-removed-while-parked": Shape(
        (IMPLEMENT, GATE, REWORK, REVIEW, VERIFY), parked_config=MODEL_REMOVED,
        differs={"rework": CONTROL, "verify": CONTROL},
        resume_admissions=((E, None, "routing_sticky_model_removed"),)),
}

_ON_STEP = re.compile(r"you are on \[([^\]]+)\]")
_AGENT = re.compile(r"<agent:([^>]*)>")


class _Harness:
    def __init__(self, tmp_path, monkeypatch, shape: Shape):
        self.tmp_path, self.monkeypatch, self.shape = tmp_path, monkeypatch, shape
        self.trace: list[tuple[str, str, str]] = []
        self.transcripts: list[str] = []
        self.sessions: list = []
        self.admissions: list[tuple] = []

    def _set_config(self, config):
        if config is None:
            self.monkeypatch.delenv("BOBI_METRICS_EXPERIMENT_JSON", raising=False)
        else:
            self.monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(config))

    def _brain(self):
        trace, transcripts, sessions = self.trace, self.transcripts, self.sessions

        unstartable, unstartable_agent = self.shape.unstartable, self.shape.unstartable_agent

        class Client(FakeBrainClient):
            started = False

            async def connect(self):
                if self.model == unstartable and unstartable_agent in ("", self.agent):
                    raise RuntimeError(f"unknown model {self.model}")
                self.started = True
                await super().connect()

            def __init__(self, agent, model, resume):
                super().__init__()
                self.agent, self.model = agent, model
                # A resumed session continues its transcript; a new one starts its own.
                self.session_id = resume or f"provider-session-{len(sessions) + 1}"
                sessions.append(self)

            async def query(self, text):
                await super().query(text)
                trace.append((_ON_STEP.search(text).group(1), self.agent, self.model))
                transcripts.append(self.session_id)

            async def receive_response(self):
                from bobi.brain import AssistantText, TurnResult
                yield AssistantText(text="Done.")
                yield TurnResult(session_id=self.session_id)

        class Brain:
            def make_session(self, **kwargs):
                agent = _AGENT.search(kwargs["system_prompt"]["append"]).group(1)
                return Client(agent, kwargs["options"]["model"], kwargs.get("resume"))

        return Brain()

    def continuity(self) -> list[int]:
        """Each step's transcript, numbered in order of first use."""
        order: dict[str, int] = {}
        return [order.setdefault(transcript, len(order)) for transcript in self.transcripts]

    def idle_sessions(self) -> int:
        """Provider sessions connected that never ran a step."""
        return sum(1 for session in self.sessions if session.started and not session.queries)

    def _runtime(self):
        from bobi.metrics.runtime import MetricsRuntime

        runtime = MetricsRuntime(self.root, mode="enabled")
        admit = runtime.admit_route

        def spy(session_name, decision, **kwargs):
            self.admissions.append((kwargs["role"], decision.model_selected if decision else None,
                                    None if decision else kwargs["fallback_reason"]))
            return False if self.shape.refuse_admission else admit(session_name, decision, **kwargs)

        self.monkeypatch.setattr(runtime, "admit_route", spy)
        self.monkeypatch.setattr("bobi.metrics.routing.get_runtime", lambda: runtime)
        return runtime

    def _process(self, call):
        runtime = self._runtime()
        with ExitStack() as stack:
            for target, kwargs in [
                ("bobi.workflow.orchestrator.get_registry", {"return_value": MagicMock()}),
                ("bobi.workflow.orchestrator._emit_lifecycle_event", {}),
                ("bobi.workflow.orchestrator._setup_worktree", {"return_value": str(self.root)}),
                ("bobi.workflow.orchestrator.log_activity", {}),
                ("bobi.brain.turns.log_activity", {}),
            ] + ([] if self.shape.saved_session else [
                # A saved session keeps the provider session id the brain reported, as in production.
                ("bobi.workflow.orchestrator.load_session_id", {"return_value": ""}),
                ("bobi.workflow.orchestrator.save_session_id", {}),
                ("bobi.brain.turns.save_session_id", {}),
            ]):
                stack.enter_context(patch(target, **kwargs))
            call()
        assert runtime.close(timeout=2)

    def run(self, *, gated: bool):
        shape = self.shape
        self.root = _bind_runtime_root(self.tmp_path / ("gated" if gated else "straight"), self.monkeypatch)
        paths.agent_yaml_path(self.root).write_text(
            "agent: test\nentry_point: manager\n"
            f"roles:\n  reviewer:\n    model: {shape.reviewer_model}\n")
        self.monkeypatch.setenv("BOBI_BRAIN_MODEL", ENGINEER_MODEL)
        self.monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
        self.monkeypatch.setattr("bobi.prompts.resolver.resolve_agent_prompt",
                                 lambda role, *args, **kwargs: f"<agent:{role}>")
        fixed_policy(self.monkeypatch, PICK)
        brain = self._brain()
        self.monkeypatch.setattr("bobi.brain.get_brain", lambda: brain)
        self._set_config(shape.config)
        steps, gates = [], 0
        for item in shape.steps:
            if isinstance(item, StepDef):
                steps.append(item)
                continue
            if item == GATE:
                if gated:
                    gates += 1
                    steps.append(StepDef(name=f"{GATE}{gates}", await_event="approval"))
                continue
            name, agent, model = item
            steps.append(StepDef(name=name, prompt=name, agent=agent, model=model))
        wf = Workflow(name="t", steps=steps)
        phases = []
        self._process(lambda: run_workflow(wf, task="t", repo="r", cwd=str(self.root),
                                           run_key="run-1", role=shape.role, model=shape.model))
        phases.append(self.admissions)
        for _ in range(2 * gates):
            [run] = WorkflowRun.list_runs(root=self.root)
            if run.status != "waiting":
                break
            if shape.legacy_record:
                # A run parked before checkpoints named the route's agent and the last agent.
                runtime_scope = run.variable_scopes["_runtime"]
                runtime_scope.pop("agent", None)
                if runtime_scope.get("route"):
                    del runtime_scope["route"]["agent"]
                run.save()
            if shape.parked_config != "unchanged":
                self._set_config(shape.parked_config)
            self.admissions = []
            self._process(lambda: resume_workflow(run, wf))
            phases.append(self.admissions)
        [run] = WorkflowRun.list_runs(root=self.root)
        assert run.status == ("failed" if shape.fails else "completed")
        route = run.variable_scopes.get("_runtime", {}).get("route")
        final_route = (route.get("agent", E), route["decision"]["model_selected"] if "decision" in route
                       else route["model"]) if route else None
        return list(self.trace), phases, final_route


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
def test_a_gate_never_changes_a_routed_runs_models(tmp_path, monkeypatch, shape):
    straight, gated = _Harness(tmp_path, monkeypatch, shape), _Harness(tmp_path, monkeypatch, shape)
    straight_trace, [straight_admissions], straight_route = straight.run(gated=False)
    gated_trace, [start, *resumes], gated_route = gated.run(gated=True)

    expected_trace = [(step, agent, shape.differs.get(step, model)) for step, agent, model in straight_trace]
    assert gated_trace == expected_trace
    assert start == straight_admissions
    # A resume records again exactly what its run recorded at its start, and decides nothing.
    reused = tuple(straight_admissions)
    assert resumes and all(tuple(resume) == (reused if shape.resume_admissions is None else shape.resume_admissions)
                       for resume in resumes)
    assert gated_route == straight_route
    # Every provider session runs a step, and with a saved transcript each
    # step continues the same transcript as without the gate.
    assert straight.idle_sessions() == gated.idle_sessions() == 0
    if shape.saved_session:
        assert gated.continuity() == straight.continuity()


@pytest.mark.parametrize("fresh", [False, True], ids=["resumes-transcript", "fresh"])
def test_redispatch_under_a_run_key_with_a_saved_transcript(tmp_path, monkeypatch, fresh):
    """A new run under a finished run's key continues its saved transcript
    without a route (the policy is not asked over an existing transcript); a
    ``fresh`` launch drops the transcript and is routed like any new run."""
    from bobi.sdk import load_session_id

    shape = Shape((IMPLEMENT, REVIEW, REWORK), saved_session=True)
    harness = _Harness(tmp_path, monkeypatch, shape)
    first_trace, [first_admissions], _ = harness.run(gated=False)
    assert first_admissions == [(E, PICK, None)]
    [finished] = WorkflowRun.list_runs(root=harness.root)
    assert load_session_id(finished.session_name, root=harness.root)
    harness.admissions = []
    wf = Workflow(name="t", steps=[StepDef(name=name, prompt=name, agent=agent) for name, agent, _ in shape.steps])
    harness._process(lambda: run_workflow(wf, task="t again", repo="r", cwd=str(harness.root),
                                          run_key="run-1", fresh=fresh))
    redispatch = harness.trace[len(first_trace):]
    engineer_model = PICK if fresh else ENGINEER_MODEL
    assert redispatch == [("implement", E, engineer_model), ("review", R, REVIEWER_MODEL),
                          ("rework", E, engineer_model)]
    assert harness.admissions == ([(E, PICK, None)] if fresh else [])
    second = next(run for run in WorkflowRun.list_runs(root=harness.root) if run.run_id != finished.run_id)
    route = second.variable_scopes["_runtime"]["route"]
    assert (route["decision"]["model_selected"] if route else None) == (PICK if fresh else None)
