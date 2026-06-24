"""Guards scripts/render-team-deps.py — the release-rollout helper that decides
whether a fleet instance rebuilds its OWN team image (C24) or rolls the shared
generic image (release.yml). A regression here would let a framework
release clobber a team-flavored instance (e.g. moda-eng-team) with the generic
image, stripping its baked tools."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "render-team-deps.py"


def _run(name: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), name],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    out = REPO / "dist" / "team-deps"
    for f in out.glob("*.sh"):
        f.unlink()


def test_team_flavored_deployment_renders_hook():
    # eng-team declares a build: spec (generic nodejs/npm/jq + the gh CLI)
    # → a team-deps path + a real rendered hook. The house toolchain pins
    # (gstack/codex/playwright) live in the private moda-eng-team overlay.
    rel = _run("eng-team")
    assert rel == "dist/team-deps/eng-team.sh"
    hook = (REPO / rel).read_text()
    assert "verify gh" in hook


def test_generic_deployment_prints_nothing():
    # canary is a team-url generic instance → no hook (rolls the shared image).
    assert _run("canary") == ""


def test_unknown_deployment_prints_nothing():
    assert _run("does-not-exist") == ""
