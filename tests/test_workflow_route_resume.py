"""A gate must not change which model a routed workflow runs, nor what it records.

Each shape runs twice through the real orchestrator and the real metrics
runtime, once straight through and once parked at approval gates and resumed:
every executed step's (step, agent, model), the route admitted at the start,
the route left in the final checkpoint, each turn's projected routing record
and the metrics summary's routing figures must match. In both runs every
recorded decision names the model its turn ran, and only the route's turns
carry one. Shapes whose routing config changes while the run is parked state
where and why they differ, as docs/JEV_ROUTER_DATA_FLOW.md section 4.2
describes.
"""

import json
import re
from collections import Counter
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
# A route condition answered from the shape's scripted verdicts, in order.
VERDICT = "${{event.verdict}} == 'approve'"
# Every turn reports this usage, so the summary can price it.
TURN_INPUT_TOKENS, TURN_OUTPUT_TOKENS = 1_000_000, 100_000


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
    # The unstartable model starts failing only once this step has run.
    unstartable_after: str = ""
    # (model, step): that step's turns on that model fail without running a tool.
    failing_turn: tuple = ()
    # The brain continues a transcript on another model.
    cross_model: bool = False
    # Answers to VERDICT route conditions, in evaluation order.
    verdicts: tuple = ()
    reviewer_model: str = REVIEWER_MODEL
    # The run is expected to fail, in both runs alike.
    fails: bool = False
    # Where a config change while parked, or a checkpoint written before the
    # last agent was recorded, is documented to differ from the straight run:
    # {step: model or (agent, model)} after the gate, and each resume's admissions.
    differs: dict = field(default_factory=dict)
    resume_admissions: tuple | None = None


def _s(name, agent="", model=""):
    return (name, agent, model)


def _route(name, condition, goto, else_goto="", **kwargs):
    return StepDef(name=name, condition=condition, goto=goto, else_goto=else_goto, **kwargs)


IMPLEMENT, REWORK, VERIFY, FIX = _s("implement", E), _s("rework", E), _s("verify", E), _s("fix", E)
REVIEW, REVIEW2 = _s("review", R), _s("review2", R)
TUNE_PINNED = _s("tune", E, "opus")
NOTIFY_DONE = StepDef(name="notify_done", notify="slack", message="done")


def _approval(goto="notify_done", else_goto="implement", **kwargs):
    return _route("approval_route", VERDICT, goto, else_goto, max_iterations=3, **kwargs)


# A route that jumps over the first prompt step to a step pinned to another model.
JUMP_TO_TUNE = _route("j", "true", "tune")
TUNE_STEP = _s("tune", E, "gpt-step")

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
    # A checkpoint written before the last agent was recorded resumes under the
    # rule it was written with: the first agent named from the resume point on.
    "forward-jump-to-agentless-step-old-checkpoint": Shape(
        (IMPLEMENT, REVIEW, GATE, _route("decide", "true", "polish"), _s("tail", E), _s("polish")),
        legacy_record=True, differs={"polish": (E, PICK)}),
    "old-checkpoint-agentless-step-after-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, _s("polish"), REWORK), legacy_record=True, differs={"polish": (E, PICK)}),
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
    # Only the route's turns are recorded as routed: another agent's turns and
    # a step pinned to its own model run outside the route.
    "other-agents-and-pinned-step-are-not-routed": Shape(
        (IMPLEMENT, GATE, REVIEW, REVIEW2, GATE, TUNE_PINNED), saved_session=True),
    # A reject loop takes the route back to an earlier prompt step behind a gate.
    "reject-loop-behind-gate-one-back-edge": Shape(
        (IMPLEMENT, REVIEW, GATE, _approval(), NOTIFY_DONE), verdicts=("reject", "approve")),
    "reject-loop-behind-gate-one-back-edge-saved-session": Shape(
        (IMPLEMENT, REVIEW, GATE, _approval(), NOTIFY_DONE), verdicts=("reject", "approve"),
        saved_session=True),
    "reject-loop-behind-gate-two-back-edges": Shape(
        (IMPLEMENT, REVIEW, GATE, _approval(), NOTIFY_DONE), verdicts=("reject", "reject", "approve")),
    "reject-loop-across-two-gates": Shape(
        (IMPLEMENT, GATE, REVIEW, GATE, _approval(), NOTIFY_DONE), verdicts=("reject", "reject", "approve"),
        saved_session=True),
    "reject-loop-to-other-agent-behind-gate": Shape(
        (IMPLEMENT, REVIEW, GATE, _approval(else_goto="review"), NOTIFY_DONE, REWORK),
        verdicts=("reject", "approve")),
    "reject-loop-back-to-gate-first-route-step": Shape(
        (GATE, IMPLEMENT, REVIEW, _approval(), NOTIFY_DONE), verdicts=("reject", "reject", "approve")),
    # The policy's model fails the route's first step after a jump to a step
    # pinned to another model: the control rerun starts a fresh transcript.
    "first-route-step-fails-after-pinned-step-cross-model": Shape(
        (JUMP_TO_TUNE, IMPLEMENT, TUNE_STEP, GATE, VERIFY, _s("done", E)), failing_turn=(PICK, "verify"),
        cross_model=True, saved_session=True),
    "first-route-step-fails-after-pinned-step-cross-model-gate-first": Shape(
        (GATE, JUMP_TO_TUNE, IMPLEMENT, TUNE_STEP, VERIFY, _s("done", E)), failing_turn=(PICK, "verify"),
        cross_model=True, saved_session=True),
    # A failed policy model falls back to control only on the route's first
    # step, before or after a gate alike.
    "policy-model-unstartable-after-first-route-step": Shape(
        (IMPLEMENT, GATE, REVIEW, REWORK, VERIFY), unstartable=PICK, unstartable_after="review", fails=True),
    "policy-model-unstartable-after-first-route-step-saved-session": Shape(
        (IMPLEMENT, GATE, REVIEW, REWORK, VERIFY), unstartable=PICK, unstartable_after="review", fails=True,
        saved_session=True),
    "first-route-step-fails-behind-gate": Shape((GATE, IMPLEMENT, VERIFY), failing_turn=(PICK, "implement")),
    "first-route-step-fails-behind-gate-saved-session": Shape(
        (GATE, IMPLEMENT, VERIFY), failing_turn=(PICK, "implement"), saved_session=True),
}

_ON_STEP = re.compile(r"you are on \[([^\]]+)\]")
_AGENT = re.compile(r"<agent:([^>]*)>")


def _route_agent(shape: Shape) -> str:
    """The agent the run's route is decided for: the launch role, else the first agent named."""
    named = (item.agent if isinstance(item, StepDef) else item[1] for item in shape.steps if item != GATE)
    return shape.role or next((agent for agent in named if agent), "")


def _pinned_steps(shape: Shape) -> set[str]:
    return {item[0] for item in shape.steps if isinstance(item, tuple) and item[2]}


class _Harness:
    def __init__(self, tmp_path, monkeypatch, shape: Shape):
        self.tmp_path, self.monkeypatch, self.shape = tmp_path, monkeypatch, shape
        self.trace: list[tuple[str, str, str]] = []
        self.transcripts: list[str] = []
        self.prompts: list[str] = []
        # The process (0 = launch, then one per resume) that ran each turn.
        self.turn_process: list[int] = []
        self.sessions: list = []
        self.admissions: list[tuple] = []
        self.process = 0

    def _set_config(self, config):
        if config is None:
            self.monkeypatch.delenv("BOBI_METRICS_EXPERIMENT_JSON", raising=False)
        else:
            self.monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(config))

    def _brain(self):
        harness, shape = self, self.shape
        trace, transcripts, sessions = self.trace, self.transcripts, self.sessions

        class Client(FakeBrainClient):
            started = False

            async def connect(self):
                if (self.model == shape.unstartable and shape.unstartable_agent in ("", self.agent)
                        and (not shape.unstartable_after
                             or any(step == shape.unstartable_after for step, _, _ in trace))):
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
                self.step = _ON_STEP.search(text).group(1)
                trace.append((self.step, self.agent, self.model))
                transcripts.append(self.session_id)
                harness.prompts.append(text)
                harness.turn_process.append(harness.process)

            async def receive_response(self):
                from bobi.brain import AssistantText, TurnResult
                from bobi.brain.base import BrainInvocation, BrainUsage

                usage = BrainUsage(model=self.model, provider_event_id=f"call-{len(trace)}",
                                   input_tokens=TURN_INPUT_TOKENS, output_tokens=TURN_OUTPUT_TOKENS)
                invocation = BrainInvocation(provider_event_id=usage.provider_event_id,
                                             model=self.model, usage=usage)
                if shape.failing_turn == (self.model, self.step):
                    yield TurnResult(session_id=self.session_id, is_error=True, error_kind="api_error",
                                     error_message="model overloaded", usage=[usage],
                                     invocations=[invocation])
                    return
                yield AssistantText(text="Done.")
                yield TurnResult(session_id=self.session_id, usage=[usage], invocations=[invocation])

        class Brain:
            def make_session(self, **kwargs):
                agent = _AGENT.search(kwargs["system_prompt"]["append"]).group(1)
                return Client(agent, kwargs["options"]["model"], kwargs.get("resume"))

        if shape.cross_model:
            from bobi.brain.base import BrainCapabilities

            Brain.capabilities = BrainCapabilities(cross_model_resume=True)
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
        # Turns are observed by the process runtime, as in production.
        self.monkeypatch.setattr("bobi.metrics.runtime._runtime", runtime)
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
        self.process += 1

    def _scripted_verdicts(self):
        from bobi.workflow.variables import VariableContext

        verdicts, evaluate = list(self.shape.verdicts), VariableContext.evaluate_condition

        def scripted(ctx, expr):
            return verdicts.pop(0) == "approve" if expr == VERDICT else evaluate(ctx, expr)

        self.monkeypatch.setattr(VariableContext, "evaluate_condition", scripted)

    def workflow(self, *, gated: bool) -> Workflow:
        steps, gates = [], 0
        for item in self.shape.steps:
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
        return Workflow(name="t", steps=steps)

    def setup(self, *, gated: bool):
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
        self._scripted_verdicts()
        self._set_config(shape.config)

    def resume_parked(self, wf: Workflow, phases: list, *, before_resume=None):
        """Resume the parked run until it finishes; each resume is a new process."""
        for _ in range(12):
            [run] = WorkflowRun.list_runs(root=self.root)
            if run.status != "waiting":
                return
            if before_resume is not None:
                before_resume(run)
            if self.shape.parked_config != "unchanged":
                self._set_config(self.shape.parked_config)
            self.admissions = []
            self._process(lambda: resume_workflow(run, wf))
            phases.append(self.admissions)
        raise AssertionError("the run never left its gates")

    def run(self, *, gated: bool):
        shape = self.shape
        self.setup(gated=gated)
        wf = self.workflow(gated=gated)
        phases = []
        self._process(lambda: run_workflow(wf, task="t", repo="r", cwd=str(self.root),
                                           run_key="run-1", role=shape.role, model=shape.model))
        phases.append(self.admissions)

        def legacy(run):
            # A run parked before checkpoints named the route's agent and the last agent.
            runtime_scope = run.variable_scopes["_runtime"]
            runtime_scope.pop("agent", None)
            if runtime_scope.get("route"):
                del runtime_scope["route"]["agent"]
            run.save()

        self.resume_parked(wf, phases, before_resume=legacy if shape.legacy_record else None)
        [run] = WorkflowRun.list_runs(root=self.root)
        assert run.status == ("failed" if shape.fails else "completed")
        route = run.variable_scopes.get("_runtime", {}).get("route")
        final_route = (route.get("agent", E), route["decision"]["model_selected"] if "decision" in route
                       else route["model"]) if route else None
        return list(self.trace), phases, final_route

    def recorded(self):
        """Each turn's projected routing record, in turn order, and the summary's routing figures."""
        from bobi.metrics.collector import MetricsCollectorService
        from bobi.metrics.query import MetricsQueries
        from bobi.metrics.store import connect

        MetricsCollectorService(self.root).collect_once()
        connection = connect(self.root / "state/metrics/metrics.db", readonly=True)
        try:
            rows = connection.execute(
                "SELECT t.trigger_id,d.model_selected,json_extract(d.metadata_json,'$.policy.status') AS status,"
                "d.fallback_reason,d.router_reason FROM turns t LEFT JOIN router_decisions d ON d.turn_id=t.turn_id "
                "ORDER BY t.started_at_us,t.turn_id").fetchall()
        finally:
            connection.close()
        turns = [None if row["model_selected"] is None else
                 (row["model_selected"], row["status"], row["fallback_reason"], row["router_reason"])
                 for row in rows]
        assert [row["trigger_id"] for row in rows] == [step for step, _, _ in self.trace]
        routing = MetricsQueries(self.root).summary({"include_dashboard": True})["routing"]
        savings = routing["savings"]
        figures = {
            **{key: routing[key] for key in ("turns", "routed_turns", "shadow_turns", "fallback_turns",
                                              "policy_calls")},
            "models": {row["model_selected"]: row["turns"] for row in routing["models"]},
            **{key: savings[key] for key in ("routed_cost_usd", "ceiling_cost_usd", "saved_usd", "saved_pct")},
        }
        return turns, figures

    def check_records(self, turns, figures, phases):
        """Every decision names the model its turn ran, and only the route's
        turns carry a decision or a fallback."""
        stored = not self.shape.refuse_admission
        decided = [stored and any(model for _, model, _ in phase) for phase in phases]
        # A process that admitted no decision but a reason runs the route on a fallback.
        fell_back = [stored and not any(model for _, model, _ in phase) and any(reason for _, _, reason in phase)
                     for phase in phases]
        route_agent, pinned = _route_agent(self.shape), _pinned_steps(self.shape)
        fallbacks = 0
        for (step, agent, model), process, record in zip(self.trace, self.turn_process, turns):
            on_route = not self.shape.model and step not in pinned and agent == route_agent
            assert (record is not None) == (on_route and decided[process]), (step, agent, model, record)
            if record is not None:
                assert record[0] == model, (step, record)
            fallbacks += (record is not None and record[2] is not None) or (on_route and fell_back[process])
        routed = [record for record in turns if record is not None and record[2] is None]
        assert figures["routed_turns"] == len(routed)
        assert (figures["fallback_turns"] or 0) == fallbacks
        assert figures["models"] == dict(Counter(record[0] for record in turns if record is not None))


def _as_straight(records: list, processes: list[int]) -> list:
    """Gated records with a resumed process's re-admitted ``reused`` status read as the straight run's."""
    return [record if record is None or process == 0 else (*record[:1], "*", *record[2:])
            for record, process in zip(records, processes)]


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
def test_a_gate_never_changes_a_routed_runs_models(tmp_path, monkeypatch, shape):
    straight, gated = _Harness(tmp_path, monkeypatch, shape), _Harness(tmp_path, monkeypatch, shape)
    straight_trace, [straight_admissions], straight_route = straight.run(gated=False)
    straight_turns, straight_figures = straight.recorded()
    gated_trace, phases, gated_route = gated.run(gated=True)
    gated_turns, gated_figures = gated.recorded()
    start, *resumes = phases

    def expected(step, agent, model):
        differs = shape.differs.get(step, model)
        return (step, *differs) if isinstance(differs, tuple) else (step, agent, differs)

    assert gated_trace == [expected(*turn) for turn in straight_trace]
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
    straight.check_records(straight_turns, straight_figures, [straight_admissions])
    gated.check_records(gated_turns, gated_figures, phases)
    if shape.parked_config == "unchanged" and not shape.differs:
        # A resumed process re-admits the run's decision as reused; every
        # other recorded field and every summary figure is the straight run's.
        assert all(record[1] == "reused" for record, process in zip(gated_turns, gated.turn_process)
                   if record is not None and process > 0)
        assert _as_straight(gated_turns, gated.turn_process) == _as_straight(straight_turns, gated.turn_process)
        assert gated_figures == straight_figures


def test_only_the_routes_turns_are_recorded_and_priced_as_routed(tmp_path, monkeypatch):
    """Another agent's turns and a step pinned to its own model are not the
    route's: they record no decision, and the summary prices the route's turn
    alone against the strongest candidate."""
    harness = _Harness(tmp_path, monkeypatch, Shape((IMPLEMENT, REVIEW, REVIEW2, TUNE_PINNED),
                                                    reviewer_model=ENGINEER_MODEL))
    trace, _, _ = harness.run(gated=False)
    assert trace == [("implement", E, PICK), ("review", R, "opus"), ("review2", R, "opus"), ("tune", E, "opus")]
    turns, figures = harness.recorded()
    assert turns == [(PICK, "decided", None, "policy_selected"), None, None, None]
    assert figures == {"turns": 4, "routed_turns": 1, "shadow_turns": 0, "fallback_turns": 0, "policy_calls": 1,
                       "models": {PICK: 1}, "routed_cost_usd": 1.5, "ceiling_cost_usd": 3.0, "saved_usd": 1.5,
                       "saved_pct": 50.0}


def test_control_rerun_of_a_continued_transcript_gets_the_run_background(tmp_path, monkeypatch):
    """The route's first step continues a pinned step's transcript on another
    model and fails there; its control rerun opens a fresh transcript, which
    gets the launch input exactly as a fresh transcript of a straight run does."""
    steps = (JUMP_TO_TUNE, IMPLEMENT, TUNE_STEP, VERIFY, _s("done", E))
    continued = _Harness(tmp_path / "continued", monkeypatch,
                         Shape(steps, failing_turn=(PICK, "verify"), cross_model=True, saved_session=True))
    assert continued.run(gated=False)[0] == [("tune", E, "gpt-step"), ("verify", E, PICK),
                                             ("verify", E, CONTROL), ("done", E, CONTROL)]
    assert continued.continuity() == [0, 0, 1, 1]
    fresh = _Harness(tmp_path / "fresh", monkeypatch, Shape(steps, failing_turn=(PICK, "verify")))
    assert fresh.run(gated=False)[0] == continued.trace
    background = "Workflow `t` background for run `run-1`"
    assert background not in continued.prompts[1]
    assert continued.prompts[2] == fresh.prompts[1] == fresh.prompts[2]
    assert continued.prompts[2].startswith(background) and "task: t" in continued.prompts[2]


def test_a_run_parked_by_an_older_build_resumes_as_that_build_resumed_it(tmp_path, monkeypatch):
    """A checkpoint written before the last agent was recorded resumes an
    agent-less step as the older build did: as the first agent named from the
    resume point on, not as the agent of a step the run jumped over.

    The checkpoint is the one the older orchestrator writes: its _checkpoint
    (b9bc83bc bobi/workflow/orchestrator.py:926-940) saves only model,
    launch_model, launch_effort and visits. The expected trace is what that
    build's resume_workflow ran for this checkpoint, with routing off."""
    shape = Shape((_s("s1", E), _route("j", "true", "land"), _s("skipped", R), _s("land"), GATE,
                   _s("after"), _s("tail", E)), config=None, saved_session=True)
    harness = _Harness(tmp_path, monkeypatch, shape)
    harness.setup(gated=True)
    wf = harness.workflow(gated=True)
    harness._process(lambda: run_workflow(wf, task="t", repo="r", cwd=str(harness.root), run_key="run-1"))
    assert harness.trace == [("s1", E, ENGINEER_MODEL), ("land", E, ENGINEER_MODEL)]

    def written_by_older_build(run):
        saved = run.variable_scopes["_runtime"]
        run.variable_scopes["_runtime"] = {"model": saved["model"], "launch_model": "", "launch_effort": "",
                                           "visits": saved["visits"]}
        assert run.variable_scopes["_runtime"] == {
            "model": ENGINEER_MODEL, "launch_model": "", "launch_effort": "",
            "visits": {"s1": 1, "j": 1, "land": 1, "gate1": 1}}
        run.save()

    harness.resume_parked(wf, [], before_resume=written_by_older_build)
    assert harness.trace[2:] == [("after", E, ENGINEER_MODEL), ("tail", E, ENGINEER_MODEL)]
    assert harness.continuity() == [0, 0, 0, 0]


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
