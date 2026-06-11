"""Workflow schema — YAML parsing and dataclasses.

A workflow is a linear sequence of steps. Each step is either:
- A prompt step: injects a prompt into the agent, waits for handoff
- A route step: deterministic branch based on handoff outputs
- An await step: suspends the workflow waiting for an external event
- A notify step: deterministic notification (e.g. Slack message)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class HandoffContract:
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)


@dataclass
class StepDef:
    name: str
    prompt: str = ""
    agent: str = ""
    handoff: HandoffContract = field(default_factory=HandoffContract)
    timeout: int = 1800
    worktree: bool = False

    # Route step fields
    condition: str = ""
    goto: str = ""
    else_goto: str = ""

    # Await step fields
    await_event: str = ""

    # Notify step fields
    notify: str = ""         # notification target (e.g. "slack")
    message: str = ""        # message template (supports ${{scope.key}})


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
            handoff=handoff,
            timeout=s.get("timeout", 1800),
            worktree=s.get("worktree", False),
            condition=s.get("if", ""),
            goto=s.get("goto", ""),
            else_goto=s.get("else", ""),
            await_event=s.get("await", ""),
            notify=s.get("notify", ""),
            message=s.get("message", ""),
        )
        steps.append(step)

    return Workflow(
        name=raw.get("name", path.stem),
        steps=steps,
        trigger=raw.get("trigger", ""),
        description=raw.get("description", ""),
    )
