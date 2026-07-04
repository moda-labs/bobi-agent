"""Shared loader for GitHub workflow assertions (test_ci_workflows,
test_release_workflow, and future workflow test modules)."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_workflow(name: str) -> dict:
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / name).read_text()
    )
