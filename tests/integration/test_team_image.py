"""Integration tests for team-flavored images (C24 / #368).

Proves the team-deps mechanism end-to-end against a real Docker build, using a
SYNTHETIC tiny build spec (not eng-team's real gstack `./setup`, which is slow +
network-flaky). Two halves of the contract:

  1. BUILD: the rendered team-deps hook runs as a layer below the framework
     wheel, bakes ~-relative tools into the IMAGE home (/home/bobi), and the
     `verify: requires` step re-runs the team's checks against that same home —
     so a passing build IS the verification, on the exact path the runtime uses.
  2. RUNTIME: $HOME stays on the image, so baked tools are read in place — never
     copied onto a volume. Only Claude's durable state moves to the volume via
     CLAUDE_CONFIG_DIR, and the entrypoint points the whole ~/.claude at it (with
     baked skills surfaced under it from an image path). This closes the
     build-time-vs-runtime HOME trap by construction (build HOME == runtime HOME,
     no seed copy to silently drop a verified file) AND makes ~/.claude fully
     coincide with Claude's real state, so tools keying off ~/.claude/{projects,
     settings.json,skills,…} or $HOME just work.

Gated on a Docker daemon (the `docker` marker, excluded from integration-fast).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest

from bobi.build_render import load_team_config, render_team_deps_script

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.docker

# A baked tool we can assert on without any network: write a fake skill via
# ~/.claude/skills (the path gstack uses) and verify it via a requires check.
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

# The build's ~/.claude/skills symlink redirects the write to the immutable image
# path (outside ~/.claude). At runtime the agent reaches it two ways that must
# both resolve: via CLAUDE_CONFIG_DIR/skills and via ~/.claude (-> the config dir).
BAKED_SKILL = "/opt/bobi/skills/faketool/SKILL.md"
CONFIG_SKILL = "/data/claude/skills/faketool/SKILL.md"
HOME_SKILL = "/home/bobi/.claude/skills/faketool/SKILL.md"
CODEX_CONFIG_SKILL = "/data/codex/skills/faketool/SKILL.md"
CODEX_HOME_SKILL = "/home/bobi/.codex/skills/faketool/SKILL.md"


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

    tag = "bobi-teamtest:pytest"
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
def test_team_deps_baked_outside_dotclaude(team_image: str):
    """The hook ran below the wheel: skills bake to the immutable image path
    OUTSIDE ~/.claude (so the entrypoint can repoint ~/.claude at the volume),
    and there is NO seed dir or tool stamp (the old copy-to-volume model is
    gone)."""
    proc = _run(
        "docker", "run", "--rm", "--entrypoint", "sh", team_image, "-c",
        f"test -e {BAKED_SKILL} "
        "&& ! test -e /opt/bobi/home-seed "
        "&& ! test -e /home/bobi/.bobi-tool-stamp && echo OK",
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


@requires_docker
@pytest.mark.timeout(2500)
def test_runtime_home_fully_coincides_with_config_dir(team_image: str):
    """At boot $HOME stays on the image; the entrypoint points ~/.claude at the
    volume config dir and surfaces the baked skills under it. The contract this
    proves — the thing that makes arbitrary tools "just work":

      1. the baked skill resolves BOTH via CLAUDE_CONFIG_DIR/skills AND via
         ~/.claude/skills (one coherent home tree), and
      2. a NON-skills path written to Claude's real state dir
         (CLAUDE_CONFIG_DIR/projects, where transcripts live) is readable via
         ~/.claude/projects — the exact path gstack-memory-ingest walks. The
         old skills-only bridge left that seam open; full coincidence closes it.
    """
    vol = "bobi-teamtest-vol"
    cname = "bobi-teamtest-run"
    _run("docker", "rm", "-f", cname)
    _run("docker", "volume", "rm", "-f", vol)
    _run("docker", "volume", "create", vol)
    try:
        # api_key mode just needs a non-empty key to clear the entrypoint's auth
        # guard; no team source → after the symlinks it parks in the wait-for-team
        # loop, which is fine — the ~/.claude wiring happens before that.
        up = _run(
            "docker", "run", "-d", "--name", cname,
            "-v", f"{vol}:/data",
            "-e", "ANTHROPIC_API_KEY=sk-not-real",
            "-e", "BOBI_AUTH=api_key",
            team_image,
        )
        assert up.returncode == 0, up.stderr

        # ~/.claude wiring happens before the wait-for-team loop; poll for the
        # skill resolving through it.
        resolved = False
        for _ in range(30):
            chk = _run("docker", "exec", cname, "test", "-e", HOME_SKILL)
            if chk.returncode == 0:
                resolved = True
                break
            time.sleep(1)
        logs = _run("docker", "logs", cname).stdout + _run("docker", "logs", cname).stderr
        assert resolved, f"skill never resolved via ~/.claude.\nLogs:\n{logs[-3000:]}"

        # (1) Same baked file reachable via BOTH the config dir and ~/.claude.
        both = _run("docker", "exec", cname, "sh", "-c",
                    f"test -e {CONFIG_SKILL} && test -e {HOME_SKILL} && echo OK")
        assert "OK" in both.stdout, both.stdout + both.stderr
        # ~/.claude is the whole volume config dir (not just a skills bridge).
        link = _run("docker", "exec", cname, "readlink", "/home/bobi/.claude")
        assert link.stdout.strip() == "/data/claude", link.stdout

        # (2) The seam that motivated full coincidence: a file written to Claude's
        # real transcripts dir is visible at the ~/.claude/projects path tools use.
        seam = _run("docker", "exec", cname, "sh", "-c",
                    "mkdir -p /data/claude/projects && echo hi > /data/claude/projects/probe.txt "
                    "&& cat /home/bobi/.claude/projects/probe.txt")
        assert seam.stdout.strip() == "hi", seam.stdout + seam.stderr
    finally:
        _run("docker", "rm", "-f", cname)
        _run("docker", "volume", "rm", "-f", vol)


@requires_docker
@pytest.mark.timeout(2500)
def test_codex_runtime_sees_baked_team_skills(team_image: str):
    """Codex sessions look under their durable home (/data/codex), not Claude's
    config dir. The entrypoint must surface the same baked team skills there so
    issue-lifecycle gates such as review/qa/browse resolve without replacing
    Codex's own skills tree.
    """
    vol = "bobi-teamtest-codex-vol"
    cname = "bobi-teamtest-codex"
    _run("docker", "rm", "-f", cname)
    _run("docker", "volume", "rm", "-f", vol)
    _run("docker", "volume", "create", vol)
    try:
        seed = _run("docker", "run", "--rm", "-v", f"{vol}:/data",
                    "--entrypoint", "sh", team_image, "-c",
                    "mkdir -p /data/project/.bobi /data/codex/skills/.system/existing "
                    "/data/custom-review "
                    "&& printf 'agent: pytest-team\\nbrain:\\n  kind: codex\\n' > /data/project/.bobi/agent.yaml "
                    "&& echo keep > /data/codex/skills/.system/existing/SKILL.md "
                    "&& ln -s /data/custom-review /data/codex/skills/review "
                    "&& ln -s /opt/bobi/skills/removed-old /data/codex/skills/removed-old")
        assert seed.returncode == 0, seed.stdout + seed.stderr

        up = _run(
            "docker", "run", "-d", "--name", cname,
            "-v", f"{vol}:/data",
            "-e", "OPENAI_API_KEY=sk-not-real",
            "-e", "BOBI_AUTH=api_key",
            team_image,
        )
        assert up.returncode == 0, up.stderr

        resolved = False
        for _ in range(30):
            chk = _run("docker", "exec", cname, "test", "-e", CODEX_HOME_SKILL)
            if chk.returncode == 0:
                resolved = True
                break
            time.sleep(1)
        logs = _run("docker", "logs", cname).stdout + _run("docker", "logs", cname).stderr
        assert resolved, f"skill never resolved via ~/.codex.\nLogs:\n{logs[-3000:]}"

        both = _run("docker", "exec", cname, "sh", "-c",
                    f"test -e {CODEX_CONFIG_SKILL} && test -e {CODEX_HOME_SKILL} "
                    "&& test -e /data/codex/skills/.system/existing/SKILL.md "
                    "&& ! test -L /data/codex/skills/removed-old && echo OK")
        assert "OK" in both.stdout, both.stdout + both.stderr
        link = _run("docker", "exec", cname, "readlink", "/home/bobi/.codex")
        assert link.stdout.strip() == "/data/codex", link.stdout
        skills_link = _run("docker", "exec", cname, "readlink", "/data/codex/skills/faketool")
        assert skills_link.stdout.strip() == "/opt/bobi/skills/faketool", skills_link.stdout
        custom_link = _run("docker", "exec", cname, "readlink", "/data/codex/skills/review")
        assert custom_link.stdout.strip() == "/data/custom-review", custom_link.stdout
    finally:
        _run("docker", "rm", "-f", cname)
        _run("docker", "volume", "rm", "-f", vol)
