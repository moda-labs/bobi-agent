"""Workflow dispatcher — surfaces workflows to the manager for semantic matching.

Workflow resolution order (most specific wins):
  1. <project>/.modastack/workflows/   — project-specific overrides
  2. <modastack>/workflows/            — built-in defaults
"""

from __future__ import annotations

import logging
from pathlib import Path

from .schema import Workflow, load_workflow

log = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).parent


class WorkflowDispatcher:

    def __init__(self):
        self.workflows: list[tuple[Workflow, str]] = []

    def load_all_workflows(self, project_path: Path | None = None,
                           agent_name: str | None = None):
        """Load workflows: installed .modastack/workflows/ → built-in fallback."""
        if project_path is None:
            from modastack.sdk import get_project_root
            project_path = get_project_root()

        if project_path:
            installed_wf_dir = project_path / ".modastack" / "workflows"
            if installed_wf_dir.exists():
                self._load_from(installed_wf_dir, source=agent_name or str(project_path))

        self._load_from(WORKFLOWS_DIR, source="default")

    def _load_from(self, directory: Path, source: str):
        if not directory.exists():
            return
        for yaml_file in directory.glob("*.yaml"):
            try:
                wf = load_workflow(yaml_file)
                self.workflows.append((wf, source))
                log.info(f"Loaded workflow: {wf.name} (trigger: {wf.trigger}, source: {source})")
            except Exception as e:
                log.error(f"Failed to load {yaml_file}: {e}")

    def find_workflow(self, name: str) -> Workflow | None:
        """Find a workflow by name."""
        for wf, _source in self.workflows:
            if wf.name == name:
                return wf
        return None

    def format_workflow_menu(self) -> str:
        """Format all loaded workflows as a menu for the manager prompt.

        Deduplicates by name — most-specific source wins (project > default).
        Returns a formatted string the manager can use to decide which workflow
        to run for a given event.
        """
        seen: dict[str, tuple[Workflow, int]] = {}
        for wf, source in self.workflows:
            priority = 0 if source == "default" else 1
            prev = seen.get(wf.name)
            if prev is None or priority > prev[1]:
                seen[wf.name] = (wf, priority)

        if not seen:
            return "No workflows loaded."

        lines = ["Available workflows (pick by name):\n"]
        for wf, _priority in seen.values():
            trigger = wf.trigger.strip()
            desc = wf.description.strip()
            lines.append(f"- {wf.name} — {trigger}")
            if desc and desc != trigger:
                lines.append(f"  {desc}")
            lines.append("")

        return "\n".join(lines)
