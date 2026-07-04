"""Workflow schema — YAML parsing and dataclasses.

A workflow is a linear sequence of steps. Each step is either:
- A prompt step: injects a prompt into the agent, waits for handoff
- A route step: deterministic branch based on handoff outputs
- An await step: suspends the workflow waiting for an external event
- A notify step: deterministic notification (e.g. Slack message)
- A native action step: runs a registered Python function, no LLM
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ROUTE_LOOP_MAX_ITERATIONS = 3


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


@dataclass
class Workflow:
    name: str
    steps: list[StepDef]
    trigger: str = ""
    description: str = ""

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

    workflow = Workflow(
        name=raw.get("name", path.stem),
        steps=steps,
        trigger=raw.get("trigger", ""),
        description=raw.get("description", ""),
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
