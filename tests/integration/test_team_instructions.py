"""Stub e2e for team-shipped global instructions (#779).

Installs a package that ships a root-level ``AGENTS.md`` and boots a REAL
manager on the stub brain, asserting the applicable global-instruction targets
materialize with the managed block before the manager reaches idle. The risk
in this feature is file materialization on the boot path, not brain behavior -
brain auto-load of these paths is vendor-documented - so the stub leg is the
acceptance bar and no claude leg is needed (per CLAUDE.md's judgement call).
"""

import os

import pytest

from tests.integration.conftest import _provision_bobi_env

HOUSE_RULES = "# House rules\n\nAlways write tests first.\n"


@pytest.fixture(scope="module")
def instructions_bobi_env(tmp_path_factory):
    """A stub-brain env whose installed package ships AGENTS.md.

    Its own scaffold (not the shared ``stub_bobi_env``): every manager boot in
    the shared env would otherwise render instructions into whatever HOME the
    surrounding test left in place.
    """
    old_home = os.environ.get("BOBI_HOME")
    old_root = os.environ.get("BOBI_ROOT")
    env = _provision_bobi_env(
        tmp_path_factory.mktemp("bobi-instructions"),
        agent_name="test-repo", brain="stub", agents_md=HOUSE_RULES,
    )
    try:
        yield env
    finally:
        if old_home is None:
            os.environ.pop("BOBI_HOME", None)
        else:
            os.environ["BOBI_HOME"] = old_home
        if old_root is None:
            os.environ.pop("BOBI_ROOT", None)
        else:
            os.environ["BOBI_ROOT"] = old_root


@pytest.fixture
def bobi_env(instructions_bobi_env):
    """The name the autouse binder recognizes (binds paths + stub-brain pins)."""
    return instructions_bobi_env


@pytest.mark.timeout(120)
def test_boot_renders_global_instructions(bobi_env, monkeypatch, tmp_path):
    """Boot to idle → every target the stub brain applies to (~/AGENTS.md)
    carries the managed block with the package content."""
    from bobi.brain.instructions import MANAGED_BEGIN, MANAGED_END
    from bobi.service import launch_team, stop_team

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Pre-existing operator content must survive the render.
    (fake_home / "AGENTS.md").write_text("# operator notes\n")
    monkeypatch.setenv("HOME", str(fake_home))

    installed = bobi_env.package_dir / "AGENTS.md"
    assert installed.read_text() == HOUSE_RULES  # install froze the file
    try:
        launch_team(bobi_env.project_path, wait_timeout=60)
        text = (fake_home / "AGENTS.md").read_text()
        assert MANAGED_BEGIN in text and MANAGED_END in text
        assert "Always write tests first." in text
        assert text.startswith("# operator notes")
    finally:
        # stop_team SIGTERMs and waits for exit, unlinking the pid file.
        stop_team(bobi_env.project_path)
