"""End-to-end acceptance for the agent-bootstrapped dependency model (#428 Stage 3).

Proves the whole cold path against a REAL Docker build and a REAL brain, driving
the exact CI entry point (`scripts/build-team-images.sh`):

  1. A team declares an ARBITRARY dependency loosely — a `guide:` + a required
     `success:`, no pinned `install:`.
  2. build-team-images.sh detects the guide-only dep, builds a fresh base image,
     and runs the bootstrap AGENT inside it: the agent reads the guide, installs
     the dependency (pinning versions, adapting to the image), and reports the
     exact steps as a recipe.
  3. That recipe is frozen through the ONE renderer into the team image's build
     layer, and the image's `verify: requires` re-checks `success` at build time.
  4. The dependency is present and working in the built image.

The test target is `cowsay` (a tiny, real Debian package NOT in the base image):
the agent must figure out `apt-get install cowsay` on its own from the guide. That
is the "arbitrary dependency, no framework recipe" contract the ticket is about.

Gated on BOTH a Docker daemon and a Claude key (`ANTHROPIC_API_KEY`), so it runs
in the heavy container/claude CI suite, not the fast PR lane. Slow (a base image
build + a live agent + a team build): budget accordingly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.docker, pytest.mark.claude]

# A loose, framework-recipe-free declaration: only a guide + a success contract.
GUIDE_TEAM = """
    agent: dep-e2e
    tool_library:
      - name: cowsay
        guide: |
          Install the `cowsay` command-line tool from the Debian `cowsay` apt
          package, system-wide (as root), so the `cowsay` binary is on PATH.
        success: command -v cowsay
"""

TEAM = "dep-e2e"
E2E_TAG = "pytest"                              # pin TAG so cleanup is deterministic
BASE_TAG = f"bobi-bootstrap-base:{E2E_TAG}"    # built by build-team-images.sh
TEAM_IMG = f"bobi-e2e/bobi-dep-e2e:{E2E_TAG}"  # REGISTRY=bobi-e2e, TAG=E2E_TAG
TEAM_IMG_LATEST = "bobi-e2e/bobi-dep-e2e:latest"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


requires_docker = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")
requires_claude_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY required for the live bootstrap agent")


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def bootstrapped_team_image():
    """Drive build-team-images.sh over a synthetic guide-only team, end to end.

    Places the team inside the repo build context (under dist/, gitignored),
    runs the real CI build script (base image build → in-container agent bootstrap
    → team image build with the frozen recipe), and yields the team image tag.
    """
    team_root = REPO_ROOT / "dist" / "e2e"
    team_dir = team_root / "agents" / TEAM
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "agent.yaml").write_text(dedent(GUIDE_TEAM))

    env = dict(os.environ)
    env.update({
        "PUSH": "0",
        "REGISTRY": "bobi-e2e",
        "TAG": E2E_TAG,
        "BUILD_MODE": "source",
        "BOBI_BOOTSTRAP_BRAINS": "claude",  # verify under one real brain
        "IS_SANDBOX": "1",
    })
    # build-team-images.sh calls bare `python`; make it the test interpreter (which
    # has bobi importable), not whatever bare `python` resolves to (the #576 gap).
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "build-team-images.sh"),
             str(team_dir)],
            capture_output=True, text=True, env=env, timeout=3000,
        )
        if proc.returncode != 0:
            pytest.fail(
                "build-team-images.sh failed (base build, bootstrap agent, or the "
                f"image verify: requires):\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")
        yield TEAM_IMG
    finally:
        shutil.rmtree(team_root, ignore_errors=True)
        _run("docker", "image", "rm", "-f", TEAM_IMG, TEAM_IMG_LATEST, BASE_TAG)


@requires_docker
@requires_claude_key
@pytest.mark.timeout(3100)
def test_arbitrary_guide_dep_is_present_and_working_in_the_image(bootstrapped_team_image):
    """The loosely-declared dependency the agent bootstrapped is really in the
    built image and runs — proving guide → live bootstrap → frozen recipe →
    working image, with no per-tool recipe in the framework."""
    proc = _run("docker", "run", "--rm", "--entrypoint", "sh",
                bootstrapped_team_image, "-c", "command -v cowsay && cowsay moo")
    assert proc.returncode == 0, f"cowsay missing/broken:\n{proc.stdout}\n{proc.stderr}"
    assert "moo" in proc.stdout


@requires_docker
@requires_claude_key
@pytest.mark.timeout(3100)
def test_image_carries_the_dependency_set_stamp(bootstrapped_team_image):
    """The built image stamps the declared dependency-set hash (#428) so a later
    deploy/warm-boot can detect a changed set and re-bootstrap."""
    proc = _run("docker", "run", "--rm", "--entrypoint", "sh",
                bootstrapped_team_image, "-c", "cat /opt/bobi/dep-list.hash")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "dep-list.hash is empty"
