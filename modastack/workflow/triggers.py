"""Workflow dispatcher — surfaces workflows to the manager for semantic matching.

Workflow resolution order (most specific wins):
  1. <target-repo>/.modastack/workflows/   — repo-specific
  2. ~/.modastack/workflows/               — user overrides
  3. <modastack>/workflows/                — built-in defaults
"""

from __future__ import annotations

import logging
from pathlib import Path

from modastack.config import GlobalConfig

from .schema import Workflow, load_workflow

log = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "workflows"
USER_WORKFLOWS_DIR = Path.home() / ".modastack" / "workflows"

_SOURCE_PRIORITY = {"default": 0, "user": 1}


class WorkflowDispatcher:

    def __init__(self):
        self.workflows: list[tuple[Workflow, str]] = []

    def load_all_workflows(self):
        """Load workflows from all sources: repo-local, user, built-in defaults."""
        config = GlobalConfig.load()

        for repo_path in config.repos:
            repo_wf_dir = repo_path / ".modastack" / "workflows"
            if repo_wf_dir.exists():
                self._load_from(repo_wf_dir, source=str(repo_path))

        if USER_WORKFLOWS_DIR.exists():
            self._load_from(USER_WORKFLOWS_DIR, source="user")

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

        Deduplicates by name — most-specific source wins (repo > user > default).
        Returns a formatted string the manager can use to decide which workflow
        to run for a given event.
        """
        seen: dict[str, tuple[Workflow, int]] = {}
        for wf, source in self.workflows:
            priority = _SOURCE_PRIORITY.get(source, 2)
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
