"""Workflow dispatcher — surfaces workflows to the manager for semantic matching.

Workflows resolve exclusively from the installed pack image:
  <project>/.modastack/workflows/
"""

from __future__ import annotations

import logging
from pathlib import Path

from .schema import Workflow, load_workflow

log = logging.getLogger(__name__)


class WorkflowDispatcher:

    def __init__(self):
        self.workflows: list[tuple[Workflow, str]] = []

    def load_all_workflows(self, project_path: Path | None = None,
                           agent_name: str | None = None):
        """Load workflows from the installed pack at .modastack/workflows/.

        With no explicit project_path this reads from the bound
        installation root — an unbound process raises rather than
        silently loading nothing.
        """
        from modastack import paths
        root = project_path if project_path is not None else paths.modastack_root()
        installed_wf_dir = paths.workflows_dir(root)
        if installed_wf_dir.exists():
            self._load_from(installed_wf_dir, source=agent_name or str(root))

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
