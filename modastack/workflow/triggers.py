"""Workflow dispatcher — matches events to workflows.

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

    def match_event(self, event: dict) -> Workflow | None:
        """Find the best matching workflow for an event."""
        event_type = event.get("type", "")
        event_repo = event.get("data", {}).get("repo", "")

        best: Workflow | None = None
        best_specificity = -1

        for wf, source in self.workflows:
            if wf.trigger != event_type:
                continue

            if source == "default":
                specificity = 0
            elif source == "user":
                specificity = 1
            else:
                if not event_repo or not self._repo_matches(event_repo, source):
                    continue
                specificity = 2

            if specificity > best_specificity:
                best = wf
                best_specificity = specificity

        return best

    @staticmethod
    def _repo_matches(event_repo: str, source: str) -> bool:
        if event_repo == source:
            return True
        if "/" in event_repo:
            repo_name = event_repo.split("/")[-1]
            return Path(source).name == repo_name
        return Path(event_repo).name == Path(source).name
