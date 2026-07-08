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
