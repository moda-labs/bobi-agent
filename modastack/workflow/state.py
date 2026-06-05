"""Workflow run state — JSON persistence for resume support."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

def _runs_dir() -> Path:
    from modastack.sdk import get_repo_root
    root = get_repo_root()
    if not root:
        raise RuntimeError("repo root not set — call set_repo_root() first")
    d = root / ".modastack" / "state" / "workflow" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    suspended_at_step: int = -1
    await_event: str = ""
    session_name: str = ""
    variable_scopes: dict = field(default_factory=dict)
    repo: str = ""
    cwd: str = ""
    issue_id: str = ""
    resumed_at: str = ""

    def save(self):
        path = _runs_dir() / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: serialize first, write to a temp file, then
        # rename over the target. A process killed mid-write (e.g. a
        # systemctl restart during self-update) can no longer leave behind
        # a truncated 0-byte run file.
        data = json.dumps(asdict(self), indent=2)
        tmp = path.with_name(f".{self.run_id}.json.tmp")
        tmp.write_text(data)
        tmp.replace(path)

    @classmethod
    def load(cls, run_id: str) -> WorkflowRun:
        path = _runs_dir() / f"{run_id}.json"
        data = json.loads(path.read_text())
        run = cls(
            run_id=data["run_id"],
            workflow_name=data["workflow_name"],
            trigger_event=data["trigger_event"],
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            status=data.get("status", "running"),
            suspended_at_step=data.get("suspended_at_step", -1),
            await_event=data.get("await_event", ""),
            session_name=data.get("session_name", ""),
            variable_scopes=data.get("variable_scopes", {}),
            repo=data.get("repo", ""),
            cwd=data.get("cwd", ""),
            issue_id=data.get("issue_id", ""),
            resumed_at=data.get("resumed_at", ""),
        )
        for nid, ns_data in data.get("nodes", {}).items():
            run.nodes[nid] = NodeState(**ns_data)
        return run

    @classmethod
    def find_active(cls, workflow_name: str, event_key: str) -> WorkflowRun | None:
        if not _runs_dir().exists():
            return None
        for path in _runs_dir().glob("*.json"):
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
    def find_waiting(cls, await_event: str, issue_id: str = "") -> WorkflowRun | None:
        """Find a run suspended and waiting for a specific event type."""
        if not _runs_dir().exists():
            return None
        for path in _runs_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") != "waiting":
                    continue
                if data.get("await_event") != await_event:
                    continue
                if issue_id:
                    trigger_data = data.get("trigger_event", {}).get("data", {})
                    if trigger_data.get("issue_id") != issue_id:
                        continue
                return cls.load(data["run_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    @classmethod
    def find_completed(cls, workflow_name: str, event_key: str) -> WorkflowRun | None:
        if not _runs_dir().exists():
            return None
        for path in _runs_dir().glob("*.json"):
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
        if not _runs_dir().exists():
            return []
        runs = []
        for path in sorted(_runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
