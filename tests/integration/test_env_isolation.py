"""The isolated-home contract every integration test depends on.

Regression guard for a silent cross-fixture stomp: ``stub_bobi_env`` is
module-scoped and ``claude_bobi_env`` is session-scoped, yet both used to
save/restore the same process-global ``BOBI_HOME``. On a dual-brain module the
stub leg runs first and captures ``BOBI_HOME=None``, so its MODULE teardown
popped the variable while the session-scoped Claude env was still live. Every
later test that spawned a subprocess then inherited an unset ``BOBI_HOME`` and
resolved the developer's REAL ``~/.bobi``.

It failed silently: the child exited with "Bobi Agent 'test-repo' is not
installed", the parent only saw an unparseable result, and the symptom
surfaced as an unrelated-looking assertion in the sleep-cycle flow. These
tests assert the contract directly so the next stomp names itself.
"""

import os
from pathlib import Path

import pytest


class TestIsolatedHomeContract:
    def test_bobi_home_points_at_the_isolated_env(self, bobi_env):
        """BOBI_HOME must be the fixture's home, never the developer's."""
        assert os.environ.get("BOBI_HOME") == str(bobi_env.home_dir), (
            "BOBI_HOME is not bound to the isolated fixture home; a subprocess "
            "spawned now would resolve the real ~/.bobi. Got "
            f"{os.environ.get('BOBI_HOME')!r}, want {str(bobi_env.home_dir)!r}"
        )
        assert Path(os.environ["BOBI_HOME"]) != Path.home() / ".bobi"

    def test_a_subprocess_resolves_the_isolated_agent(self, bobi_env):
        """The binding must survive into a child process, which is where the
        original bug actually bit - in-process resolution looked fine."""
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c",
             "from bobi import paths; print(paths.home_dir())"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(bobi_env.home_dir), (
            f"child resolved {out.stdout.strip()!r}, "
            f"want {str(bobi_env.home_dir)!r}")
