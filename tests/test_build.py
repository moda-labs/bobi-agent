"""Unit tests for team package resolution (bobi/build.py).

The image-build engine moved behind the deploy-plugin boundary (#707); its
tests live in bobi_deploy/tests/test_build.py. What stays public is the
resolution seam (`resolve_team_dir`) and composed-render coverage through the
public dep-render seam (`bobi.dep_bootstrap.render_team_deps` - the exact
path the in-container bootstrap drives, which is why it must keep working
from the public package alone).
"""

from pathlib import Path
from textwrap import dedent

import pytest

from bobi import build as B

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_project(tmp_path: Path, agent_yaml: str) -> Path:
    """A minimal project root with one team under agents/."""
    project = tmp_path / "project"
    pkg = project / "agents" / "eng-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(dedent(agent_yaml))
    return project


GENERIC_TEAM = """
    agent: eng-team
"""


def test_resolve_team_dir_accepts_a_literal_path(tmp_path):
    project = _make_project(tmp_path, GENERIC_TEAM)
    team = tmp_path / "somewhere" / "myteam"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text("agent: myteam\n")
    assert B.resolve_team_dir(project, str(team)) == team.resolve()


def test_resolve_team_dir_prefers_local_agents_dir(tmp_path):
    project = _make_project(tmp_path, GENERIC_TEAM)
    resolved = B.resolve_team_dir(project, "eng-team")
    assert resolved == (project / "agents" / "eng-team").resolve()


def test_unresolvable_team_is_a_build_error(tmp_path):
    with pytest.raises(B.BuildError):
        B.resolve_team_dir(tmp_path, str(tmp_path / "nope"))


def test_real_eng_team_composed_render():
    """The real checked-in eng-team renders its composed deps hook - with the
    requires re-verify bake - through the public render seam (the one the
    in-container bootstrap runs). Catches compose/tool_library regressions
    against a real team."""
    from bobi.dep_bootstrap import render_team_deps

    script = render_team_deps(REPO_ROOT / "agents" / "eng-team", REPO_ROOT)
    assert script is not None
    assert "verify gh" in script
    # both in-image identity stamps render through the ONE seam
    assert "/opt/bobi/team-deps.hash" in script
