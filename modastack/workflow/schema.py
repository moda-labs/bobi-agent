"""Workflow schema — dataclasses, YAML parsing, topological sort."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class NodeType(Enum):
    BASH = "bash"
    ACTION = "action"
    PROMPT = "prompt"
    MANAGER = "manager"
    APPROVAL = "approval"
    GATE = "gate"


@dataclass
class TriggerDef:
    event: str
    filter: dict[str, Any] = field(default_factory=dict)

    def matches(self, event: dict) -> bool:
        if event.get("type") != self.event:
            return False
        data = event.get("data", {})
        for key, expected in self.filter.items():
            actual = data.get(key)
            if isinstance(expected, list):
                if not isinstance(actual, list) or not set(expected) & set(actual):
                    return False
            elif actual != expected:
                return False
        return True


@dataclass
class WaitForDef:
    phase: str = ""


@dataclass
class ListenForDef:
    source: str = ""
    match: str = ""
    channel_id: str = ""


@dataclass
class BranchDef:
    when: str = ""
    goto: str = ""


@dataclass
class NodeDef:
    id: str
    type: NodeType
    label: str = ""
    depends_on: list[str] = field(default_factory=list)
    when: str = ""
    timeout: int = 300
    outputs: dict[str, str] = field(default_factory=dict)

    # bash
    command: str = ""
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)

    # action
    action: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    # prompt
    session: str = ""
    inject: str = ""
    wait_for: WaitForDef | None = None

    # manager
    prompt: str = ""
    output_format: str = "text"

    # approval
    listen_for: ListenForDef | None = None

    # gate
    branches: dict[str, BranchDef] = field(default_factory=dict)
    fallback: str = ""


@dataclass
class WorkflowDef:
    name: str
    version: int
    trigger: TriggerDef
    nodes: dict[str, NodeDef]

    def topological_order(self) -> list[str]:
        """Kahn's algorithm — returns node IDs in execution order."""
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for nid, node in self.nodes.items():
            for dep in node.depends_on:
                if dep in in_degree:
                    in_degree[nid] += 1

        queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            nid = queue.popleft()
            order.append(nid)
            for other_id, other_node in self.nodes.items():
                if nid in other_node.depends_on:
                    in_degree[other_id] -= 1
                    if in_degree[other_id] == 0:
                        queue.append(other_id)

        if len(order) != len(self.nodes):
            missing = set(self.nodes) - set(order)
            raise ValueError(f"Cycle detected involving nodes: {missing}")

        return order

    def validate(self) -> list[str]:
        errors = []
        node_ids = set(self.nodes.keys())
        for nid, node in self.nodes.items():
            for dep in node.depends_on:
                if dep not in node_ids:
                    errors.append(f"Node '{nid}' depends on unknown node '{dep}'")
            if node.type == NodeType.GATE and not node.branches:
                errors.append(f"Gate node '{nid}' has no branches")
            if node.type == NodeType.PROMPT and not node.inject:
                errors.append(f"Prompt node '{nid}' has no inject text")
            if node.type == NodeType.MANAGER and not node.prompt:
                errors.append(f"Manager node '{nid}' has no prompt")
            if node.type == NodeType.BASH and not node.command:
                errors.append(f"Bash node '{nid}' has no command")
            if node.type == NodeType.ACTION and not node.action:
                errors.append(f"Action node '{nid}' has no action")
        try:
            self.topological_order()
        except ValueError as e:
            errors.append(str(e))
        return errors


def _parse_node(node_id: str, raw: dict) -> NodeDef:
    node_type = NodeType(raw["type"])
    node = NodeDef(
        id=node_id,
        type=node_type,
        label=raw.get("label", ""),
        depends_on=raw.get("depends_on", []),
        when=raw.get("when", ""),
        timeout=raw.get("timeout", 300),
        outputs=raw.get("outputs", {}),
    )

    if node_type == NodeType.BASH:
        node.command = raw.get("command", "")
        node.cwd = raw.get("cwd", "")
        node.env = raw.get("env", {})

    elif node_type == NodeType.ACTION:
        node.action = raw.get("action", "")
        node.params = raw.get("params", {})

    elif node_type == NodeType.PROMPT:
        node.session = raw.get("session", "")
        node.inject = raw.get("inject", "")
        wf = raw.get("wait_for")
        if wf:
            node.wait_for = WaitForDef(phase=wf.get("phase", ""))
        node.outputs = raw.get("outputs", {})

    elif node_type == NodeType.MANAGER:
        node.prompt = raw.get("prompt", "")
        node.output_format = raw.get("output_format", "text")

    elif node_type == NodeType.APPROVAL:
        lf = raw.get("listen_for")
        if lf:
            node.listen_for = ListenForDef(
                source=lf.get("source", ""),
                match=lf.get("match", ""),
                channel_id=lf.get("channel_id", ""),
            )

    elif node_type == NodeType.GATE:
        for branch_name, branch_raw in raw.get("branches", {}).items():
            node.branches[branch_name] = BranchDef(
                when=branch_raw.get("when", ""),
                goto=branch_raw.get("goto", ""),
            )
        node.fallback = raw.get("fallback", "")

    return node


def load_workflow(path: Path) -> WorkflowDef:
    raw = yaml.safe_load(path.read_text())

    trigger_raw = raw.get("trigger", {})
    trigger = TriggerDef(
        event=trigger_raw.get("event", ""),
        filter=trigger_raw.get("filter", {}),
    )

    nodes = {}
    for node_id, node_raw in raw.get("nodes", {}).items():
        nodes[node_id] = _parse_node(node_id, node_raw)

    workflow = WorkflowDef(
        name=raw.get("name", path.stem),
        version=raw.get("version", 1),
        trigger=trigger,
        nodes=nodes,
    )

    errors = workflow.validate()
    if errors:
        raise ValueError(f"Workflow '{workflow.name}' validation failed: {errors}")

    return workflow
