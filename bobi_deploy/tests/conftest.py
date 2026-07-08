"""Shared fixtures for the bobi-deploy test suite.

Self-contained on purpose: this package (and its tests) moves to the private
repo at cut time, so nothing here may reach into the public repo's
tests/conftest.py.
"""

import os
import sys
from pathlib import Path

# --- Worktree safety (must run before any `import bobi_deploy`) --------
# Pin this checkout's packages ahead of an editable install that may point at
# a DIFFERENT checkout (same hazard the public tests/conftest.py guards
# against). Anchored on this file's own location so it always matches the
# tests actually being run.
_PKG_ROOT = Path(__file__).resolve().parent.parent  # bobi_deploy/
_SRC = _PKG_ROOT / "src"
_REPO_ROOT = _PKG_ROOT.parent
for _entry in (str(_SRC),) + (
        (str(_REPO_ROOT),) if (_REPO_ROOT / "bobi").is_dir() else ()):
    if _entry not in sys.path[:2]:
        sys.path.insert(0, _entry)

import pytest


def make_repo(tmp_path: Path, agent_yaml: str, *, deployments: bool = False) -> Path:
    """A minimal bobi source root (checkout: scripts/ + Dockerfile) + one team.

    The ONE scaffold for "a checkout find_repo_root accepts" - build and deploy
    suites share it so a change to the checkout markers updates both."""
    from textwrap import dedent

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "provision-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "scripts" / "destroy-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    pkg = repo / "agents" / "eng-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(dedent(agent_yaml))
    if deployments:
        (repo / "deployments").mkdir()
    return repo


@pytest.fixture(autouse=True)
def _isolate_environ():
    """No test may leak an os.environ mutation into the next.

    resolve_secret_values reads the process environment (the CI backfill
    seam), so a leaked SLACK_BOT_TOKEN / ANTHROPIC_API_KEY silently changes a
    later test's secret resolution. Snapshot and restore around every test.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
