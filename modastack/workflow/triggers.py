"""Workflow dispatcher — matches events to workflows, manages run threads."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .engine import WorkflowEngine
from .schema import WorkflowDef, load_workflow
from .state import WorkflowRun

log = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "workflows"


class WorkflowDispatcher:

    def __init__(self):
        self.workflows: list[WorkflowDef] = []
        self._active: dict[str, tuple[threading.Thread, WorkflowEngine]] = {}
        self._dispatched_events: set[int] = set()

    def load_workflows(self, directory: Path | None = None):
        directory = directory or WORKFLOWS_DIR
        if not directory.exists():
            log.warning(f"Workflows directory not found: {directory}")
            return
        for yaml_file in directory.glob("*.yaml"):
            try:
                wf = load_workflow(yaml_file)
                self.workflows.append(wf)
                log.info(f"Loaded workflow: {wf.name} (trigger: {wf.trigger.event})")
            except Exception as e:
                log.error(f"Failed to load {yaml_file}: {e}")

    def dispatch(self, event: dict) -> bool:
        """Check if an event triggers a workflow. Returns True if dispatched."""
        for wf in self.workflows:
            if not wf.trigger.matches(event):
                continue

            event_key = event.get("data", {}).get("issue_id", "")
            run_key = f"{wf.name}:{event_key}"

            if run_key in self._active:
                thread, engine = self._active[run_key]
                if thread.is_alive():
                    log.debug(f"Run already active: {run_key}")
                    self._dispatched_events.add(id(event))
                    return True

            existing = WorkflowRun.find_active(wf.name, event_key)
            if existing:
                run = existing
                log.info(f"Resuming workflow {wf.name} for {event_key} "
                        f"(run {run.run_id})")
            else:
                run = WorkflowRun.create(wf.name, event)
                run.save()
                log.info(f"Starting workflow {wf.name} for {event_key} "
                        f"(run {run.run_id})")

            engine = WorkflowEngine(wf, run)
            thread = threading.Thread(
                target=self._run_engine,
                args=(engine, run_key),
                name=f"wf-{run.run_id}",
                daemon=True,
            )
            self._active[run_key] = (thread, engine)
            thread.start()
            self._dispatched_events.add(id(event))
            return True

        return False

    def feed_event(self, event: dict):
        """Feed an event to all active workflow engines (for approval nodes)."""
        for run_key, (thread, engine) in list(self._active.items()):
            if thread.is_alive():
                engine.feed_event(event)

    def was_dispatched(self, event: dict) -> bool:
        return id(event) in self._dispatched_events

    def active_runs(self) -> list[dict]:
        result = []
        for run_key, (thread, engine) in self._active.items():
            result.append({
                "key": run_key,
                "run_id": engine.run.run_id,
                "workflow": engine.workflow.name,
                "status": engine.run.status,
                "alive": thread.is_alive(),
            })
        return result

    def _run_engine(self, engine: WorkflowEngine, run_key: str):
        try:
            engine.execute()
        except Exception as e:
            log.error(f"Workflow engine crashed for {run_key}: {e}")
            engine.run.status = "failed"
            engine.run.save()
        finally:
            if run_key in self._active:
                del self._active[run_key]
