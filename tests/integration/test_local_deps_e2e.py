"""End-to-end acceptance for local dependency materialization (#428 Stage 5).

Proves the LOCAL-dev counterpart to the container cold path: `bobi agents
install <team> --with-deps` drives a REAL brain, already present on this machine,
to make a loosely-declared dependency's `success` true on the HOST — no Docker,
no build layer, no per-tool recipe in the framework.

  1. A team declares an ARBITRARY dependency loosely: a `guide:` + a required
     `success:`, no pinned `install:`.
  2. `install --with-deps` composes the package, sees the dep is unsatisfied,
     and runs the local brain: it reads the guide and materializes the dep
     USERLAND (a small executable into `~/.local/bin`, no sudo), adapting to the
     host exactly as a human would.
  3. The dependency's `success` now passes on the host.
  4. A second `install --with-deps` re-verifies, finds it satisfied, and runs NO
     agent (idempotency).

The target is a self-contained CLI dropped into `~/.local/bin` (the "a binary
into ~/.local/bin" case the ticket calls out): hermetic, cross-platform, and
needs no package manager or network, so CI is deterministic and never escalates
to sudo. It does mutate the runner's `~/.local/bin` — that IS what Stage 5 does —
so the fixture removes the tool on teardown.

Gated on a Claude key (`ANTHROPIC_API_KEY`); runs in the claude CI suite, not the
fast PR lane. Slow (a live agent installs a tool): budget accordingly.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = [pytest.mark.claude]

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

# An unlikely-to-collide command name so a stray host binary can't fake a pass.
TOOL = "bobi-stage5-greet"

GUIDE_TEAM = f"""
    agent: local-dep-e2e
    entry_point: manager
    tool_library:
      - name: {TOOL}
        guide: |
          Provide a command-line tool named `{TOOL}` on PATH, installed WITHOUT
          sudo. A tiny executable shell script placed in `~/.local/bin` (creating
          the directory if needed and marking it executable) is a fine way to do
          this. Running `{TOOL}` must print a line containing the word `hello`.
        success: {TOOL} | grep -q hello
"""

requires_claude_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY required for the live local-install agent")


def _install(home: Path, pack: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bobi.cli", "agents", "install", str(pack),
         "--name", "localdep", "--non-interactive", *args],
        capture_output=True, text=True, timeout=1200, cwd=str(home),
        env={**_ENV, "BOBI_HOME": str(home)},
    )


@pytest.fixture
def guide_team(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    pack = tmp_path / "local-dep-e2e"
    pack.mkdir()
    (pack / "agent.yaml").write_text(dedent(GUIDE_TEAM))
    (pack / "roles" / "manager").mkdir(parents=True)
    (pack / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
    tool_path = Path.home() / ".local" / "bin" / TOOL
    try:
        yield home, pack, tool_path
    finally:
        tool_path.unlink(missing_ok=True)


@requires_claude_key
@pytest.mark.timeout(2600)
def test_with_deps_materializes_a_guide_dep_on_the_host(guide_team):
    """The loosely-declared dependency the local agent installed is really on the
    host and its `success` passes — proving guide → live local install → working
    host, with no per-tool recipe in the framework."""
    home, pack, tool_path = guide_team

    first = _install(home, pack, "--with-deps")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    assert "Dependency check" in first.stdout
    assert f"[ok] {TOOL}" in first.stdout, (
        f"dep not satisfied after install:\n{first.stdout}\n{first.stderr}")

    # The tool is genuinely present and runnable on the host.
    assert tool_path.exists(), f"{TOOL} was not installed to ~/.local/bin"
    env = {**os.environ,
           "PATH": f"{tool_path.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    run = subprocess.run([TOOL], capture_output=True, text=True, env=env)
    assert run.returncode == 0 and "hello" in run.stdout

    # Idempotency: a second --with-deps re-verifies, finds it satisfied, and
    # queues nothing to materialize.
    second = _install(home, pack, "--with-deps")
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert "already satisfied, skipping" in second.stdout
    assert "Nothing to install." in second.stdout


@requires_claude_key
@pytest.mark.timeout(120)
def test_without_with_deps_is_compose_only(guide_team):
    """A plain install never touches the dependency pass (compose-only), so the
    guide dep is NOT materialized and no brain runs."""
    home, pack, tool_path = guide_team
    result = _install(home, pack)  # no --with-deps
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Dependency check" not in result.stdout
    assert not tool_path.exists()
