"""Workflow schema — YAML parsing and dataclasses.

A workflow is a linear sequence of steps. Each step is either:
- A prompt step: injects a prompt into the agent, waits for handoff
- A route step: deterministic branch based on handoff outputs
- An await step: suspends the workflow waiting for an external event
- A notify step: deterministic notification (e.g. Slack message)
- A native action step: runs a registered Python function, no LLM
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bobi.config import positive_int


DEFAULT_ROUTE_LOOP_MAX_ITERATIONS = 3

# The vocabulary a human answers an await gate in. It rides the resume as the
# ``event`` scope, so a route step after the await reads it as
# ``${{event.verdict}}``.
#
# Two values and no more, because every consumer has to be able to enumerate
# them: the CLI rejects anything else outright, and a workflow route treats
# everything that is not the advancing verdict - including an absent one - as
# not an approval.
GATE_VERDICT_APPROVE = "approve"
GATE_VERDICT_REJECT = "reject"
GATE_VERDICTS = (GATE_VERDICT_APPROVE, GATE_VERDICT_REJECT)


def reads_gate_verdict(workflow: "Workflow", step_idx: int) -> bool:
    """True when the step a resume would land on branches on the verdict.

    ``approve`` needs no such step: it means "continue", which is what a bare
    resume does anyway. ``reject`` does. A workflow that suspends on ``await:``
    and has an ordinary step next would take a rejection and run that step -
    advancing the work the human just refused - so a caller offering Reject has
    to be able to find out first whether the workflow can honour it.

    A substring test on the condition, deliberately: the route's expression is
    author-written text, and the only thing worth asserting is that it consults
    the verdict at all. Which way it branches is the workflow author's call and
    is covered by their own tests (see the shipped route in
    ``agents/eng-team/workflows/issue-lifecycle.yaml``).
    """
    if step_idx < 0 or step_idx >= len(workflow.steps):
        return False
    step = workflow.steps[step_idx]
    return bool(step.condition) and "event.verdict" in step.condition


@dataclass
class HandoffContract:
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)


@dataclass
class StepDef:
    name: str
    prompt: str = ""
    agent: str = ""
    model: str = ""
    effort: str = ""
    # Per-session turn cap override (#845). 0 = inherit the acting role's /
    # team's cap (see bobi.brain.resolve_max_turns). Expressed per step for the
    # same reason as ``timeout``: a build step's budget is nothing like a
    # triage step's.
    max_turns: int = 0
    handoff: HandoffContract = field(default_factory=HandoffContract)
    timeout: int = 1800
    worktree: bool = False

    # Route step fields
    condition: str = ""
    goto: str = ""
    else_goto: str = ""
    max_iterations: int = 0
    on_exhausted: str = ""

    # Await step fields
    await_event: str = ""

    # Notify step fields
    notify: str = ""         # notification target (e.g. "slack")
    message: str = ""        # message template (supports ${{scope.key}})

    # Native action step fields
    action: str = ""         # registered action name (e.g. "cleanup_worktree")


# Period vocabulary -> strftime bucket. The bucket is the run identity for one
# period of a periodic workflow: every dispatcher derives the same run_key from
# it, so the run ledger can dedupe a scheduled tick against a manual catch-up
# (issue #1048). Local time on purpose - monitor `at:` schedules are local, and
# a period boundary that disagrees with the operator's clock reads as a bug.
PERIOD_FORMATS = {
    "hourly": "%Y-%m-%dT%H",
    "daily": "%Y-%m-%d",
    "weekly": "%G-W%V",
    "monthly": "%Y-%m",
}


@dataclass
class Workflow:
    name: str
    steps: list[StepDef]
    trigger: str = ""
    description: str = ""
    # Declared run period ("daily", ...). Empty = not periodic. The workflow
    # field owns the period (decision 2026-08-18, #1048): the run_key for a
    # periodic workflow is always derived from it, never caller-chosen.
    period: str = ""

    def period_run_key(self, now: float | None = None) -> str:
        """The run identity for the current period, e.g. ``standup-2026-08-10``.

        Deterministic for every dispatcher within one period, which is what
        lets admission dedupe across dispatch paths. Derive ONCE per dispatch
        (at launch admission) and pass the result along; deriving again later
        can straddle a period boundary and mint a second identity.
        """
        if self.period not in PERIOD_FORMATS:
            # load_workflow validates this for YAML-born workflows; a
            # programmatically built one gets the same readable error here
            # instead of a bare KeyError three frames from anything named.
            raise ValueError(
                f"Workflow {self.name}: period {self.period!r} must be one "
                f"of {sorted(PERIOD_FORMATS)}"
            )
        fmt = PERIOD_FORMATS[self.period]
        bucket = time.strftime(fmt, time.localtime(now))
        return f"{self.name}-{bucket}"

    def steps_fingerprint(self) -> str:
        """A short digest of the ordered step-name list.

        Stamped onto a run's checkpoint so a retry can tell whether
        ``checkpoint_step`` still indexes the workflow it was recorded
        against - a bare index into an edited step list resumes at the wrong
        step.
        """
        import hashlib
        names = "\n".join(s.name for s in self.steps)
        return hashlib.sha256(names.encode()).hexdigest()[:12]

    def step_by_name(self, name: str) -> StepDef | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def step_index(self, name: str) -> int:
        for i, s in enumerate(self.steps):
            if s.name == name:
                return i
        return -1


def load_workflow(path: Path) -> Workflow:
    """Parse a workflow YAML file into a Workflow dataclass."""
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raise ValueError("workflow file is empty")
    if not isinstance(raw, dict):
        raise ValueError("workflow file must contain a YAML mapping")

    steps = []
    for s in raw.get("steps", []):
        handoff_raw = s.get("handoff", {})
        handoff = HandoffContract(
            required=handoff_raw.get("required", []),
            optional=handoff_raw.get("optional", []),
        )

        step = StepDef(
            name=s["name"],
            prompt=s.get("prompt", ""),
            agent=s.get("agent", ""),
            model=s.get("model", ""),
            effort=s.get("effort", ""),
            max_turns=positive_int(s.get("max_turns")),
            handoff=handoff,
            timeout=s.get("timeout", 1800),
            worktree=s.get("worktree", False),
            condition=s.get("if", ""),
            goto=s.get("goto", ""),
            else_goto=s.get("else", ""),
            max_iterations=_parse_max_iterations(s),
            on_exhausted=s.get("on_exhausted", ""),
            await_event=s.get("await", ""),
            notify=s.get("notify", ""),
            message=s.get("message", ""),
            action=s.get("action", ""),
        )
        steps.append(step)

    period = raw.get("period", "") or ""
    if period and period not in PERIOD_FORMATS:
        raise ValueError(
            f"Workflow {raw.get('name', path.stem)}: period {period!r} "
            f"must be one of {sorted(PERIOD_FORMATS)}"
        )

    workflow = Workflow(
        name=raw.get("name", path.stem),
        steps=steps,
        trigger=raw.get("trigger", ""),
        description=raw.get("description", ""),
        period=period,
    )
    _validate_back_edges(workflow)
    return workflow


def _parse_max_iterations(raw_step: dict[str, Any]) -> int:
    """Return the configured visit cap for a step.

    ``max_visits`` is accepted as an alias because the workflow problem is
    fundamentally a repeated step visit guard; the stored field uses the ticket's
    primary spelling, ``max_iterations``.
    """
    if "max_iterations" in raw_step:
        raw_value = raw_step["max_iterations"]
    elif "max_visits" in raw_step:
        raw_value = raw_step["max_visits"]
    else:
        return 0

    if isinstance(raw_value, bool):
        raise ValueError(
            f"Step {raw_step.get('name', '<unknown>')}: "
            "max_iterations must be a positive integer"
        )
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str):
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError(
                f"Step {raw_step.get('name', '<unknown>')}: "
                "max_iterations must be a positive integer"
            ) from exc
    else:
        raise ValueError(
            f"Step {raw_step.get('name', '<unknown>')}: "
            "max_iterations must be a positive integer"
        )
    if value < 1:
        raise ValueError(
            f"Step {raw_step.get('name', '<unknown>')}: "
            "max_iterations must be a positive integer"
        )
    return value


def _validate_back_edges(workflow: Workflow) -> None:
    """Apply and validate route loop caps."""
    for index, step in enumerate(workflow.steps):
        if step.on_exhausted:
            exhausted_index = workflow.step_index(step.on_exhausted)
            if exhausted_index < 0:
                raise ValueError(
                    f"Workflow {workflow.name}: step {step.name} "
                    f"on_exhausted target {step.on_exhausted} was not found"
                )
            if exhausted_index <= index:
                raise ValueError(
                    f"Workflow {workflow.name}: step {step.name} "
                    f"on_exhausted target {step.on_exhausted} must be later"
                )
        for target in (step.goto, step.else_goto):
            if not target:
                continue
            target_index = workflow.step_index(target)
            if target_index < 0 or target_index > index:
                continue
            if step.max_iterations:
                continue
            step.max_iterations = DEFAULT_ROUTE_LOOP_MAX_ITERATIONS
