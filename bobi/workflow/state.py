"""Workflow run state — JSON persistence for resume support."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

def _runs_dir(root: Path | None = None) -> Path:
    """The runs directory for *root*, or the bound root when omitted.

    Only created on the write paths: a reader (the web app folds these into
    its runs view) must never mkdir inside someone else's runtime tree.
    """
    from bobi import paths
    if root is not None:
        return paths.state_path(root) / "workflow" / "runs"
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

    def save(self, root: Path | None = None):
        path = _runs_dir(root) / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: serialize first, write to a temp file, then
        # rename over the target. A process killed mid-write (e.g. a
        # systemctl restart during self-update) can no longer leave behind
        # a truncated 0-byte run file.
        data = json.dumps(asdict(self), indent=2)
        tmp = path.with_name(f".{self.run_id}.json.tmp")
        tmp.write_text(data)
        tmp.replace(path)
        # Clean up the .resuming.json left by claim() if it exists.
        resuming = path.with_name(f"{self.run_id}.resuming.json")
        if resuming.exists():
            resuming.unlink()

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

    def claim(self, root: Path | None = None) -> bool:
        """Atomically claim this run for resume.

        Atomically takes the canonical file, then validates the persisted state
        it won before publishing ``<run_id>.resuming.json``. This prevents a
        stale in-memory object from reclaiming a run that was just closed.
        """
        src = _runs_dir(root) / f"{self.run_id}.json"
        claiming = src.with_name(f"{self.run_id}.claiming.json")
        dst = src.with_name(f"{self.run_id}.resuming.json")
        try:
            os.replace(str(src), str(claiming))
        except FileNotFoundError:
            return False

        try:
            current = type(self).from_dict(json.loads(claiming.read_text()))
            if current.status != "waiting":
                os.replace(str(claiming), str(src))
                return False
            resumed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            current.status = self.status = "resuming"
            current.resumed_at = self.resumed_at = resumed_at
            tmp = src.with_name(f".{self.run_id}.claim.tmp")
            tmp.write_text(json.dumps(asdict(current), indent=2))
            os.replace(str(tmp), str(dst))
            return True
        except Exception:
            if claiming.exists() and not src.exists():
                os.replace(str(claiming), str(src))
            raise
        finally:
            if claiming.exists():
                claiming.unlink()

    @classmethod
    def close(cls, run_id: str, *, root: Path | None = None) -> WorkflowRun | None:
        """Atomically close a waiting run, losing cleanly to resume/event."""
        src = _runs_dir(root) / f"{run_id}.json"
        closing = src.with_name(f"{run_id}.closing.json")
        try:
            os.replace(str(src), str(closing))
        except FileNotFoundError:
            return None

        try:
            run = cls.from_dict(json.loads(closing.read_text()))
            if run.status != "waiting":
                os.replace(str(closing), str(src))
                return None
            run.status = "cancelled"
            run.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            run.await_event = ""
            run.suspended_at_step = -1
            data = json.dumps(asdict(run), indent=2)
            tmp = src.with_name(f".{run_id}.close.tmp")
            tmp.write_text(data)
            os.replace(str(tmp), str(src))
            return run
        except Exception:
            if closing.exists() and not src.exists():
                os.replace(str(closing), str(src))
            raise
        finally:
            if closing.exists():
                closing.unlink()

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
    def list_runs(cls, status: str | None = None,
                  root: Path | None = None) -> list[WorkflowRun]:
        runs_dir = _runs_dir(root)
        if not runs_dir.exists():
            return []
        runs = []
        for path in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
