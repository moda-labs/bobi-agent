"""Integration tests for team-flavored images (C24 / #368).

Proves the team-deps mechanism end-to-end against a real Docker build, using a
SYNTHETIC tiny build spec (not eng-team's real gstack `./setup`, which is slow +
network-flaky). Two halves of the contract:

  1. BUILD: the rendered team-deps hook runs as a layer below the framework
     wheel, installs tools into the seed HOME, and the `verify: requires` step
     re-runs the team's checks — so a passing build IS the verification.
  2. SEED: the entrypoint copies the baked seed onto the VOLUME HOME at boot, so
     ~-relative tools survive the volume remap (runtime ~ = /data/home). This is
     the regression net for the build-time-vs-runtime HOME trap that motivated
     the whole design.

Gated on a Docker daemon (the `docker` marker, excluded from integration-fast).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest

from modastack.build_render import load_team_config, render_team_deps_script

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.docker

# A baked tool we can assert on without any network: write a fake skill into the
# seed HOME and verify it via a requires check (so the build's verify step
# exercises the same ~-relative path eng-team's gstack check uses).
SYNTH_TEAM = """
    agent: pytest-team
    build:
      run:
        - "mkdir -p ~/.claude/skills/faketool && echo 'name: faketool' > ~/.claude/skills/faketool/SKILL.md"
      verify: requires
    requires:
      - name: faketool
        check: "test -e ~/.claude/skills/faketool/SKILL.md"
"""

SEEDED_FILE = "/data/home/.claude/skills/faketool/SKILL.md"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def team_image(tmp_path_factory) -> str:
    """Render a synthetic team's deps hook and build the image with it.

    The rendered script must live INSIDE the build context (repo root) so the
    Dockerfile's `COPY ${TEAM_DEPS}` can reach it; we drop it under dist/ and
    clean up after.
    """
    team_dir = tmp_path_factory.mktemp("synth-team")
    (team_dir / "agent.yaml").write_text(dedent(SYNTH_TEAM))
    cfg = load_team_config(team_dir)
    script = render_team_deps_script(cfg)

    deps_dir = REPO_ROOT / "dist" / "team-deps"
    deps_dir.mkdir(parents=True, exist_ok=True)
    deps_file = deps_dir / "pytest-team.sh"
    deps_file.write_text(script)

    tag = "modastack-teamtest:pytest"
    try:
        proc = _run(
            "docker", "build",
            "--build-arg", "TEAM_DEPS=dist/team-deps/pytest-team.sh",
            "-t", tag, str(REPO_ROOT),
            timeout=2400,
        )
        if proc.returncode != 0:
            # A failed build IS a failed `verify: requires` (among other causes).
            pytest.fail(f"team image build failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        yield tag
    finally:
        deps_file.unlink(missing_ok=True)
        _run("docker", "image", "rm", "-f", tag)


@requires_docker
@pytest.mark.timeout(2500)
def test_team_deps_baked_into_seed(team_image: str):
    """The hook ran below the wheel: the baked tool + stamp live in the seed HOME."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", team_image, "-c",
        "test -e /opt/modastack/home-seed/.claude/skills/faketool/SKILL.md "
        "&& test -e /opt/modastack/home-seed/.modastack-tool-stamp && echo OK",
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


@requires_docker
@pytest.mark.timeout(2500)
def test_entrypoint_seeds_volume_home(team_image: str):
    """At boot the entrypoint copies the seed onto the VOLUME HOME (/data/home).

    This is the trap the design exists to close: runtime ~ is the volume, so a
    ~-relative tool baked into the image must be seeded onto it or it's invisible.
    """
    vol = "modastack-teamtest-vol"
    cname = "modastack-teamtest-run"
    _run("docker", "rm", "-f", cname)
    _run("docker", "volume", "rm", "-f", vol)
    _run("docker", "volume", "create", vol)
    try:
        # api_key mode just needs a non-empty key to clear the entrypoint's auth
        # guard; no team source → after seeding it parks in the wait-for-team
        # loop, which is fine — we only assert the seed happened.
        up = _run(
            "docker", "run", "-d", "--name", cname,
            "-v", f"{vol}:/data",
            "-e", "ANTHROPIC_API_KEY=sk-not-real",
            "-e", "MODASTACK_AUTH=api_key",
            team_image,
        )
        assert up.returncode == 0, up.stderr

        # The seed runs before the wait-for-team loop; poll the volume for it.
        seeded = False
        for _ in range(30):
            chk = _run("docker", "exec", cname, "test", "-e", SEEDED_FILE)
            if chk.returncode == 0:
                seeded = True
                break
            time.sleep(1)
        logs = _run("docker", "logs", cname).stdout + _run("docker", "logs", cname).stderr
        assert seeded, f"seed never landed on the volume HOME.\nLogs:\n{logs[-3000:]}"
    finally:
        _run("docker", "rm", "-f", cname)
        _run("docker", "volume", "rm", "-f", vol)
