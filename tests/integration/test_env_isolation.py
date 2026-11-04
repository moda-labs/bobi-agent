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

The developer's own home is part of the same contract. A stub boot writes
``~/AGENTS.md`` and strips Bobi's block from both brains' global instructions,
so a test that kept the real environment changed every Claude Code and Codex
session on the machine, and still passed (#1147).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi.brain.instructions import MANAGED_BEGIN, MANAGED_END

from .conftest import PACKAGE_ROOT


class TestIsolatedHomeContract:
    def test_bobi_home_points_at_the_isolated_env(self, bobi_env):
        """BOBI_HOME must be the fixture's home, never the developer's."""
        assert os.environ.get("BOBI_HOME") == str(bobi_env.home_dir), (
            "BOBI_HOME is not bound to the isolated fixture home; a subprocess "
            "spawned now would resolve the real ~/.bobi. Got "
            f"{os.environ.get('BOBI_HOME')!r}, want {str(bobi_env.home_dir)!r}"
        )

    def test_a_subprocess_resolves_the_isolated_agent(self, bobi_env):
        """The binding must survive into a child process, which is where the
        original bug actually bit - in-process resolution looked fine."""
        out = subprocess.run(
            [sys.executable, "-c",
             "from bobi import paths; print(paths.home_dir())"],
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(bobi_env.home_dir), (
            f"child resolved {out.stdout.strip()!r}, "
            f"want {str(bobi_env.home_dir)!r}")


@pytest.fixture
def developer(tmp_path):
    """A developer's real environment, somewhere a test can watch it.

    Both brain files carry a managed block because a stub boot strips it. With
    nothing to strip, a boot that reached them would leave no trace.
    """
    home = tmp_path / "developer"
    block = f"# my notes\n\n{MANAGED_BEGIN}\nteam rules\n{MANAGED_END}\n"
    for brain_dir, name in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
        (home / brain_dir).mkdir(parents=True)
        (home / brain_dir / name).write_text(block)
    return home


def _contents(root):
    return {p: p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def _pytest_as(developer, *args, **env):
    """Run pytest in a child, as this developer would.

    A child because the guard under test is a conftest fixture, which has
    already replaced this process's HOME.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={developer.parent / 'pytest'}", *args],
        cwd=PACKAGE_ROOT,
        env={
            **os.environ,
            "HOME": str(developer),
            "CLAUDE_CONFIG_DIR": str(developer / "claude"),
            "CODEX_HOME": str(developer / "codex"),
            # Settings a developer may export that would change what runs.
            "PYTEST_ADDOPTS": "",
            "BOBI_COMMUNICATION_STYLE": "on",
            **env,
        },
        capture_output=True, text=True,
    )


def _checkout(root, test_source):
    """A throwaway checkout with this repo's conftest and one test module, for
    behavior that depends on where tests live or how they are marked."""
    tests = root / "tests"
    tests.mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "tests" / "conftest.py", tests)
    (tests / "test_generated.py").write_text(test_source)
    return tests


class TestDevelopersHomeContract:
    @pytest.mark.timeout(300)
    def test_boots_leave_it_untouched(self, developer, tmp_path):
        """A manager boot, a blocking subagent run and a detached subagent boot,
        the three ways a stub-brain test reaches the render, then the child
        entry on each real brain. The blocking run comes first in its module
        because it requests ``stub_bobi_env`` directly, so that env is
        provisioned before any per-test HOME, in this home."""
        before = _contents(developer)

        result = _pytest_as(
            developer,
            "tests/integration/test_manager_lifecycle.py::TestManagerStartStop"
            "::test_launch_team_service_starts_manager[stub]",
            "tests/integration/test_agent_launch.py::TestWaitRunsThroughTheExecutor"
            "::test_wait_prints_the_final_text_and_writes_a_ledger_entry",
            "tests/integration/test_agent_launch.py::TestAdhocAgentLaunch"
            "::test_adhoc_agent_completes[stub]",
            "tests/test_subagent.py::TestRunAgentEntryRootBinding"
            "::test_sets_process_brain_from_passed_root",
            "tests/test_subagent.py::TestRunAgentEntryRootBinding"
            "::test_clears_stale_process_brain_when_passed_root_has_default_brain",
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert _contents(developer) == before
        # All five still rendered, in their per-test homes. Without this, a
        # boot that stopped rendering would leave this test green.
        rendered = list((tmp_path / "pytest").glob("user-home[0-9]*/AGENTS.md"))
        assert len(rendered) == 5

    @pytest.mark.timeout(60)
    def test_credentialed_tests_keep_it(self, developer, tmp_path):
        """These tiers may need the developer's credentials. CI deselects all
        three, so nothing else there would notice them losing the real home."""
        tests = _checkout(tmp_path / "checkout", "from pathlib import Path\nimport pytest\n" + "".join(
            f"\n@pytest.mark.{tier}\ndef test_{tier}():\n"
            f"    assert Path.home() == Path({str(developer)!r})\n"
            for tier in ("claude", "live", "docker")
        ))

        result = _pytest_as(developer, str(tests))

        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.timeout(60)
    def test_the_systemd_module_stays_off_a_developers_machine(self, developer):
        """systemd gives its manager the real home and no fixture can reach it,
        so that module runs only where the home is disposable."""
        result = _pytest_as(
            developer, "tests/integration/test_service_systemd.py", "-rs",
            GITHUB_ACTIONS="false", BOBI_TEST_SYSTEMD="0",
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "BOBI_TEST_SYSTEMD" in result.stdout

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("tier", ["integration", "e2e"])
    def test_a_checkout_named_for_a_tier_keeps_the_unit_guards(
        self, developer, tmp_path, tier
    ):
        """``worktrees/<branch>/`` makes a checkout like this one reachable with
        a branch named ``e2e/foo``, and matching the absolute path would switch
        off the guard that hides the developer's real service units."""
        tests = _checkout(
            tmp_path / tier / "checkout",
            "from bobi import service_manager\n\n"
            "def test_guard_is_on():\n"
            '    assert "_no_service_units" in str(service_manager.systemd_path())\n',
        )

        result = _pytest_as(developer, str(tests))

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_cleared_environment_cannot_reach_it(self):
        """With HOME unset, Python falls back to the password database, which no
        variable redirects and no watched HOME can see. test_launch_lineage
        clears the environment and renders, so a unit run overwrote the real
        ~/AGENTS.md and ~/.claude/CLAUDE.md however HOME was set (#1147)."""
        isolated = Path.home()

        with patch.dict(os.environ, clear=True):
            assert Path.home() == isolated
