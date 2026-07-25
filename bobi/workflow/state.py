"""Workflow run state — JSON persistence for resume support."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bobi import fsutil


def _runs_dir() -> Path:
    from bobi import paths
    d = paths.state_dir() / "workflow" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class WorkflowRun:
    run_id: str
    workflow_name: str
    trigger_event: dict
    started_at: str = ""
    completed_at: str = ""
    status: str = "running"
    suspended_at_step: int = -1
    await_event: str = ""
    session_name: str = ""
    variable_scopes: dict = field(default_factory=dict)
    repo: str = ""
    cwd: str = ""
    run_key: str = ""
    resumed_at: str = ""

    def save(self):
        path = _runs_dir() / f"{self.run_id}.json"
        # Atomic: a process killed mid-write (e.g. a systemctl restart
        # during self-update) can't leave a truncated run file behind.
        fsutil.atomic_write_json(path, asdict(self))
        # Clean up the .resuming.json left by claim() if it exists.
        path.with_name(f"{self.run_id}.resuming.json").unlink(missing_ok=True)

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowRun:
        return cls(
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
            run_key=data.get("run_key", ""),
            resumed_at=data.get("resumed_at", ""),
        )

    @classmethod
    def load(cls, run_id: str) -> WorkflowRun:
        path = _runs_dir() / f"{run_id}.json"
        return cls.from_dict(json.loads(path.read_text()))

    def claim(self) -> bool:
        """Atomically claim this run for resume.

        Renames ``<run_id>.json`` → ``<run_id>.resuming.json`` using
        ``os.replace``.  On POSIX this is atomic — exactly one process
        wins when multiple try concurrently.  Returns True if claimed,
        False if another process already claimed it.

        The claim comes first and the status update second: a loser does
        no writes at all, so it can never disturb the winner's in-flight
        state file (the previous order had both racers writing one shared
        temp path, where the loser's cleanup could delete the winner's
        temp and make *both* claims fail).
        """
        src = _runs_dir() / f"{self.run_id}.json"
        dst = _runs_dir() / f"{self.run_id}.resuming.json"
        try:
            os.replace(str(src), str(dst))
        except FileNotFoundError:
            # Another process already renamed (claimed) the source file.
            return False
        # Claim won — record the resume on the claimed file, atomically.
        self.status = "resuming"
        self.resumed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        fsutil.atomic_write_json(dst, asdict(self))
        return True

    @classmethod
    def find_waiting(cls, await_event: str, run_key: str = "",
                     repo: str = "") -> WorkflowRun | None:
        """Find a run suspended and waiting for a specific event type.

        When *repo* is non-empty, only runs whose ``repo`` field matches
        are returned.  This prevents cross-repo collisions when multiple
        repos share the same state directory.
        """
        if not _runs_dir().exists():
            return None
        for path in _runs_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") != "waiting":
                    continue
                if data.get("await_event") != await_event:
                    continue
                if run_key:
                    trigger_data = data.get("trigger_event", {}).get("data", {})
                    if trigger_data.get("run_key") != run_key:
                        continue
                if repo and data.get("repo", "") != repo:
                    continue
                return cls.from_dict(data)
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
                runs.append(cls.from_dict(data))
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
