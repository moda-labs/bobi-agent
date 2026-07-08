"""Integration test for `bobi build` (#610): a real image from a declaration.

Acceptance (issue #610): a team declaring apt deps gets them baked and the
`verify: requires` contract enforced at build. Builds from the current
checkout in source mode; docker-gated like test_container_image.py.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from bobi_deploy.build import build_team_image

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.docker


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True,
                       timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ok(), reason="docker daemon not available"
)


@requires_docker
def test_bobi_build_bakes_and_verifies_apt_dep():
    """`bobi build <team> --tag <ref>` from a checkout: the declared apt tool
    is baked, the requires check ran at build time, and both identity stamps
    are in the image."""
    tag = "bobi-build-pytest:apt"
    with tempfile.TemporaryDirectory(prefix="bobi-build-team-") as tmp:
        team = Path(tmp) / "jq-team"
        team.mkdir()
        (team / "agent.yaml").write_text(dedent("""
            agent: jq-team
            build:
              apt: [jq]
              verify: requires
            requires:
              - name: jq
                check: "command -v jq"
        """))
        try:
            result = build_team_image(str(team), tags=[tag],
                                      project_path=REPO_ROOT)
        finally:
            # staged into the real checkout's build context; don't leave it
            (REPO_ROOT / "dist" / "team-deps" / "jq-team.sh").unlink(
                missing_ok=True)
    assert result.tags == [tag]
    assert result.team_deps == "dist/team-deps/jq-team.sh"

    def _in_image(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", cmd[0], tag, *cmd[1:]],
            capture_output=True, text=True, timeout=120)

    jq = _in_image("jq", "--version")
    assert jq.returncode == 0, jq.stderr

    # the deps-identity stamps prove the team-deps layer (and its final
    # verify step) actually ran in THIS image
    stamp = _in_image("cat", "/opt/bobi/team-deps.hash")
    assert stamp.returncode == 0 and stamp.stdout.strip(), stamp.stderr

    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
