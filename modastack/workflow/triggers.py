"""Workflow dispatcher — matches events to workflows, manages run threads.

Workflow resolution order (most specific wins):
  1. <target-repo>/.modastack/workflows/   — repo-specific
  2. ~/.modastack/workflows/               — user overrides
  3. <modastack>/workflows/                — built-in defaults

When multiple workflows match the same event, a repo-specific workflow
takes priority over a default. Within the same tier, the first match wins.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from modastack.config import GlobalConfig, RepoConfig

from .engine import WorkflowEngine
from .schema import WorkflowDef, load_workflow
from .state import WorkflowRun

log = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "workflows"
USER_WORKFLOWS_DIR = Path.home() / ".modastack" / "workflows"


class WorkflowDispatcher:

    def __init__(self):
        self.workflows: list[tuple[WorkflowDef, str]] = []  # (workflow, source)
        self._active: dict[str, tuple[threading.Thread, WorkflowEngine]] = {}
        self._dispatched_events: set[int] = set()

    def load_all_workflows(self):
        """Load workflows from all sources: repo-local, user, built-in defaults."""
        config = GlobalConfig.load()

        # 1. Repo-specific workflows (highest priority)
        for repo_path in config.repos:
            repo_wf_dir = repo_path / ".modastack" / "workflows"
            if repo_wf_dir.exists():
                self._load_from(repo_wf_dir, source=str(repo_path))

        # 2. User overrides
        if USER_WORKFLOWS_DIR.exists():
            self._load_from(USER_WORKFLOWS_DIR, source="user")

        # 3. Built-in defaults (lowest priority)
        self._load_from(WORKFLOWS_DIR, source="default")

    def load_workflows(self, directory: Path | None = None):
        """Load from a single directory (backwards compat)."""
        directory = directory or WORKFLOWS_DIR
        self._load_from(directory, source="default")

    def _load_from(self, directory: Path, source: str):
        if not directory.exists():
            return
        for yaml_file in directory.glob("*.yaml"):
            try:
                wf = load_workflow(yaml_file)
                self.workflows.append((wf, source))
                log.info(f"Loaded workflow: {wf.name} (trigger: {wf.trigger.event}, source: {source})")
            except Exception as e:
                log.error(f"Failed to load {yaml_file}: {e}")

    def _find_best_workflow(self, event: dict) -> WorkflowDef | None:
        """Find the most specific matching workflow for an event.

        Priority: repo-specific > user > default.
        A repo-specific workflow matches only if its source matches the event's repo.
        """
        event_repo = event.get("data", {}).get("repo", "")

        best: WorkflowDef | None = None
        best_specificity = -1  # 0=default, 1=user, 2=repo-match

        for wf, source in self.workflows:
            if not wf.trigger.matches(event):
                continue

            if source == "default":
                specificity = 0
            elif source == "user":
                specificity = 1
            else:
                # Repo-specific: only matches if the event repo matches the source
                if not event_repo or not self._repo_matches(event_repo, source):
                    continue
                specificity = 2

            if specificity > best_specificity:
                best = wf
                best_specificity = specificity

        return best

    def _repo_matches(self, event_repo: str, source: str) -> bool:
        """Check if an event's repo field matches a workflow source path.

        Handles both path formats (/home/ubuntu/dev/bettertab)
        and slug formats (moda-labs/bettertab).
        """
        if event_repo == source:
            return True
        # Slug match: "moda-labs/bettertab" matches source path ending in "bettertab"
        if "/" in event_repo:
            repo_name = event_repo.split("/")[-1]
            return Path(source).name == repo_name
        return Path(event_repo).name == Path(source).name

    def dispatch(self, event: dict) -> bool:
        """Check if an event triggers a workflow. Returns True if dispatched."""
        wf = self._find_best_workflow(event)
        if not wf:
            return False

        event_key = event.get("data", {}).get("issue_id", "")
        run_key = f"{wf.name}:{event_key}"

        if run_key in self._active:
            thread, engine = self._active[run_key]
            if thread.is_alive():
                log.debug(f"Run already active: {run_key}")
                self._dispatched_events.add(id(event))
                return True

        completed = WorkflowRun.find_completed(wf.name, event_key)
        if completed:
            log.debug(f"Workflow already completed for {event_key}, skipping")
            return False

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

    def cleanup_stale_runs(self):
        """Mark any in-flight workflow runs from before the restart as failed.

        Fresh assignment events will create new runs.
        """
        cleaned = 0
        for run in WorkflowRun.list_runs():
            if run.status in ("running", "waiting"):
                event_key = run.trigger_event.get("data", {}).get("issue_id", "")
                run.status = "failed"
                run.save()
                cleaned += 1
        if cleaned:
            log.info(f"Cleaned {cleaned} stale workflow run(s) from previous session")

    def _find_workflow_by_name(self, name: str) -> WorkflowDef | None:
        for wf, _source in self.workflows:
            if wf.name == name:
                return wf
        return None

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
