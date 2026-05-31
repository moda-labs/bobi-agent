"""Workflow run state — JSON persistence for resume support."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUNS_DIR = Path.home() / ".modastack" / "workflow" / "runs"


@dataclass
class NodeState:
    status: str = "pending"
    started_at: str = ""
    completed_at: str = ""
    outputs: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class WorkflowRun:
    run_id: str
    workflow_name: str
    trigger_event: dict
    nodes: dict[str, NodeState] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    status: str = "running"

    def save(self):
        path = RUNS_DIR / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, run_id: str) -> WorkflowRun:
        path = RUNS_DIR / f"{run_id}.json"
        data = json.loads(path.read_text())
        run = cls(
            run_id=data["run_id"],
            workflow_name=data["workflow_name"],
            trigger_event=data["trigger_event"],
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            status=data.get("status", "running"),
        )
        for nid, ns_data in data.get("nodes", {}).items():
            run.nodes[nid] = NodeState(**ns_data)
        return run

    @classmethod
    def find_active(cls, workflow_name: str, event_key: str) -> WorkflowRun | None:
        if not RUNS_DIR.exists():
            return None
        for path in RUNS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") not in ("running", "waiting"):
                    continue
                if data.get("workflow_name") != workflow_name:
                    continue
                trigger_data = data.get("trigger_event", {}).get("data", {})
                if trigger_data.get("issue_id") == event_key:
                    return cls.load(data["run_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    @classmethod
    def find_completed(cls, workflow_name: str, event_key: str) -> WorkflowRun | None:
        if not RUNS_DIR.exists():
            return None
        for path in RUNS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") != "completed":
                    continue
                if data.get("workflow_name") != workflow_name:
                    continue
                trigger_data = data.get("trigger_event", {}).get("data", {})
                if trigger_data.get("issue_id") == event_key:
                    return cls.load(data["run_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    @classmethod
    def list_runs(cls, status: str | None = None) -> list[WorkflowRun]:
        if not RUNS_DIR.exists():
            return []
        runs = []
        for path in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text())
                if status and data.get("status") != status:
                    continue
                runs.append(cls.load(data["run_id"]))
            except (json.JSONDecodeError, KeyError):
                continue
        return runs

    @classmethod
    def create(cls, workflow_name: str, event: dict) -> WorkflowRun:
        return cls(
            run_id=str(uuid.uuid4())[:8],
            workflow_name=workflow_name,
            trigger_event=event,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

    def node_state(self, node_id: str) -> NodeState:
        if node_id not in self.nodes:
            self.nodes[node_id] = NodeState()
        return self.nodes[node_id]

    def retry_failed(self) -> list[str]:
        """Reset all failed nodes to pending so execute() retries them.

        Also resets the run status back to running. Returns the IDs of
        nodes that were reset.
        """
        reset = []
        for nid, ns in self.nodes.items():
            if ns.status == "failed":
                ns.status = "pending"
                ns.error = ""
                ns.started_at = ""
                ns.completed_at = ""
                reset.append(nid)
        if reset:
            self.status = "running"
            self.completed_at = ""
            self.save()
        return reset
