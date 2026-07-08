"""Unit tests for `bobi build` (bobi/build.py, #610).

Docker is stubbed (build._run is monkeypatched to record commands), so nothing
here builds an image or touches a daemon. The real-build path is covered by
tests/integration/test_bobi_build.py (docker-gated).
"""

from pathlib import Path
from textwrap import dedent

import pytest

from bobi import build as B

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- fixtures ----------------------------------------------------------------

def _make_repo(tmp_path: Path, agent_yaml: str) -> Path:
    """A minimal bobi source root (checkout: scripts/ + Dockerfile) + one team."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "provision-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "scripts" / "destroy-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    pkg = repo / "agents" / "eng-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(dedent(agent_yaml))
    return repo


BAKING_TEAM = """
    agent: eng-team
    build:
      apt: [jq]
"""

GENERIC_TEAM = """
    agent: eng-team
"""

GUIDE_TEAM = """
    agent: eng-team
    tool_library:
      - name: gtool
        guide: 'install gtool somehow'
        success: 'command -v gtool'
"""



@pytest.fixture
def recorder(monkeypatch):
    """Record every build._run command; docker is assumed present."""
    calls = []

    def fake_run(cmd):
        calls.append({"cmd": cmd})

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(B, "_run", fake_run)
    monkeypatch.setattr(B.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def _builds(calls):
    return [c["cmd"] for c in calls if c["cmd"][:2] == ["docker", "build"]]


def _pushes(calls):
    return [c["cmd"] for c in calls if c["cmd"][:2] == ["docker", "push"]]


# --- source mode -------------------------------------------------------------

def test_default_tag_and_source_context(tmp_path, recorder):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    result = B.build_team_image(str(repo / "agents" / "eng-team"),
                                project_path=repo)
    (cmd,) = _builds(recorder)
    assert cmd[-1] == str(repo)                      # context = checkout root
    assert cmd[cmd.index("-f") + 1] == str(repo / "Dockerfile")
    assert cmd[cmd.index("-t") + 1] == "bobi-eng-team:latest"
    assert result.tags == ["bobi-eng-team:latest"]
    assert result.mode == "source"
    # source mode: BOBI_BUILD defaults in the Dockerfile, no build-arg needed
    assert not any(a.startswith("BOBI_BUILD=") for a in cmd)


def test_team_deps_staged_and_passed(tmp_path, recorder):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    result = B.build_team_image(str(repo / "agents" / "eng-team"),
                                project_path=repo)
    assert result.team_deps == "dist/team-deps/eng-team.sh"
    (cmd,) = _builds(recorder)
    assert "TEAM_DEPS=dist/team-deps/eng-team.sh" in cmd
    staged = repo / "dist" / "team-deps" / "eng-team.sh"
    script = staged.read_text()
    assert "apt-get install -y --no-install-recommends jq" in script
    # both in-image identity stamps render through the ONE seam
    assert "/opt/bobi/team-deps.hash" in script


def test_real_eng_team_composed_render(tmp_path):
    """The real checked-in eng-team renders its composed hook with the
    requires re-verify bake (coverage the deleted render-team-deps test
    carried; catches compose/tool_library regressions against a real team)."""
    rel = B.stage_team_deps(REPO_ROOT / "agents" / "eng-team", REPO_ROOT,
                            ctx=tmp_path, allow_agent=False)
    assert rel == "dist/team-deps/eng-team.sh"
    script = (tmp_path / rel).read_text()
    assert "verify gh" in script


def test_multi_tag_and_push(tmp_path, recorder):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    B.build_team_image(str(repo / "agents" / "eng-team"), project_path=repo,
                       tags=["ghcr.io/acme/eng:1", "ghcr.io/acme/eng:latest"],
                       push=True)
    (cmd,) = _builds(recorder)
    assert cmd[cmd.index("-t") + 1] == "ghcr.io/acme/eng:1"
    rest = cmd[cmd.index("-t") + 2:]
    assert rest[rest.index("-t") + 1] == "ghcr.io/acme/eng:latest"
    assert _pushes(recorder) == [["docker", "push", "ghcr.io/acme/eng:1"],
                                 ["docker", "push", "ghcr.io/acme/eng:latest"]]


def test_no_push_by_default(tmp_path, recorder):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    B.build_team_image(str(repo / "agents" / "eng-team"), project_path=repo)
    assert _pushes(recorder) == []


def test_generic_team_builds_plain_image(tmp_path, recorder):
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    result = B.build_team_image(str(repo / "agents" / "eng-team"),
                                project_path=repo)
    (cmd,) = _builds(recorder)
    assert result.team_deps is None
    assert not any(a.startswith("TEAM_DEPS=") for a in cmd)  # noop hook default


# --- staging contract ----------------------------------------------------------

def test_baking_team_without_ctx_is_clean_error(tmp_path):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    with pytest.raises(B.BuildError, match="no build context"):
        B.stage_team_deps(repo / "agents" / "eng-team", repo, ctx=None,
                          allow_agent=True)


def test_dockerfile_escape_hatch_refused_in_build_path(tmp_path):
    """A raw-Dockerfile team must not silently become a generic image under
    the team's tag; deploy's long-standing generic-image contract stays."""
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    team = repo / "agents" / "eng-team"
    # the escape hatch is a raw Dockerfile SIBLING of agent.yaml (config.py
    # loader sets BuildSpec.dockerfile when the file exists)
    (team / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(B.BuildError, match="Dockerfile escape hatch"):
        B.stage_team_deps(team, repo, ctx=None, allow_agent=True)
    assert B.stage_team_deps(team, repo, ctx=None, allow_agent=False) is None


# --- guide-only deps: containerized bootstrap ---------------------------------

@pytest.fixture
def bootstrap_recorder(monkeypatch):
    """Like `recorder`, but the fake `docker run` writes the rendered script
    into the mounted out dir, as the real in-container bootstrap does."""
    calls = []

    def fake_run(cmd):
        calls.append({"cmd": cmd})
        if cmd[:2] == ["docker", "run"]:
            mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
            out = next(m.split(":")[0] for m in mounts
                       if m.endswith(B._OUT_MOUNT))
            Path(out, "team-deps.sh").write_text("#!/bin/bash\n# bootstrapped\n")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(B, "_run", fake_run)
    monkeypatch.setattr(B.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_guide_dep_bootstraps_inside_base_image(tmp_path, bootstrap_recorder,
                                                monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = _make_repo(tmp_path, GUIDE_TEAM)
    team = repo / "agents" / "eng-team"
    result = B.build_team_image(str(team), project_path=repo)

    base, final = _builds(bootstrap_recorder)
    # base image: same Dockerfile/context, NO team deps, input-derived tag
    base_tag = base[base.index("-t") + 1]
    assert base_tag.startswith(f"{B.BOOTSTRAP_BASE_REPO}:")
    assert not any(a.startswith("TEAM_DEPS=") for a in base)
    # final image bakes the bootstrapped hook
    assert "TEAM_DEPS=dist/team-deps/eng-team.sh" in final
    assert "bootstrapped" in (repo / "dist" / "team-deps" /
                              "eng-team.sh").read_text()
    assert result.team_deps == "dist/team-deps/eng-team.sh"

    (run,) = [c["cmd"] for c in bootstrap_recorder
              if c["cmd"][:2] == ["docker", "run"]]
    # recipe faithful to the image, not the host: flattened team dir ro +
    # out dir are the ONLY mounts; cwd off any mount; image python runs;
    # the run targets the SAME tag the base build produced
    assert base_tag in run
    assert f"{team}:{B._TEAM_MOUNT}:ro" in run
    assert run[run.index("-w") + 1] == "/tmp"
    assert run[run.index("--entrypoint") + 1] == "python"
    assert run[run.index("-m") + 1] == "bobi.dep_bootstrap"
    assert B._TEAM_MOUNT in run
    assert run[run.index("--render") + 1] == f"{B._OUT_MOUNT}/team-deps.sh"
    assert run[run.index("--brains") + 1] == "claude"  # default: claude only
    assert "IS_SANDBOX=1" in run
    assert "ANTHROPIC_API_KEY" in run
    assert "OPENAI_API_KEY" not in run  # unset key is not forwarded


def test_bootstrap_base_tag_scopes_by_build_inputs():
    """Concurrent builds with different modes/versions must not share a base
    tag (a retag between one run's build and run would violate OQ6)."""
    a = B._bootstrap_base_tag({}, Path("/repo/Dockerfile"))
    b = B._bootstrap_base_tag({"BOBI_BUILD": "pypi", "BOBI_VERSION": "1.0"},
                              Path("/repo/Dockerfile"))
    c = B._bootstrap_base_tag({}, Path("/elsewhere/Dockerfile"))
    assert len({a, b, c}) == 3
    assert a == B._bootstrap_base_tag({}, Path("/repo/Dockerfile"))  # stable


def test_stage_team_deps_refuses_guide_deps_without_agent(tmp_path):
    repo = _make_repo(tmp_path, GUIDE_TEAM)
    with pytest.raises(B.GuideDepsError, match="gtool"):
        B.stage_team_deps(repo / "agents" / "eng-team", repo, ctx=None,
                          allow_agent=False)


# --- build modes ---------------------------------------------------------------

def test_binary_mode_pins_pypi_version(tmp_path, recorder, monkeypatch):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    team = repo / "agents" / "eng-team"
    # no checkout anywhere above the project path -> binary mode
    pkg = tmp_path / "_deploy"
    (pkg / "docker").mkdir(parents=True)
    (pkg / "Dockerfile").write_text("FROM scratch\n")
    (pkg / "docker" / "noop-deps.sh").write_text("")
    (pkg / "scripts").mkdir()
    (pkg / "scripts" / "provision-instance.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "destroy-instance.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        B, "find_repo_root",
        lambda p=None: (_ for _ in ()).throw(B.BuildError("no checkout")))
    monkeypatch.setattr(B, "_packaged_deploy_dir", lambda: pkg)
    monkeypatch.setattr(B, "_bobi_version", lambda: "9.9.9")

    result = B.build_team_image(str(team), project_path=tmp_path / "elsewhere")
    (cmd,) = _builds(recorder)
    assert "BOBI_BUILD=pypi" in cmd
    assert "BOBI_VERSION=9.9.9" in cmd
    assert result.mode == "pypi"
    # context is the staged bundle, not a repo
    assert cmd[-1] != str(repo)
    assert (Path(cmd[-1]) / "docker" / "noop-deps.sh").name == "noop-deps.sh"

    with pytest.raises(B.BuildError, match="checkout"):
        B.build_team_image(str(team), project_path=tmp_path / "elsewhere",
                           build_mode="source")
    with pytest.raises(B.BuildError, match="checkout"):
        B.build_team_image(str(team), project_path=tmp_path / "elsewhere",
                           build_mode="wheel")


def test_explicit_bobi_version_wins(tmp_path, recorder, monkeypatch):
    """The wrapper's BOBI_VERSION contract: an explicit pin overrides the
    installed version (which may be unpublished in a dev checkout)."""
    repo = _make_repo(tmp_path, BAKING_TEAM)
    monkeypatch.setattr(B, "_bobi_version", lambda: "0.0.0.dev0")
    B.build_team_image(str(repo / "agents" / "eng-team"), project_path=repo,
                       build_mode="pypi", bobi_version="0.38.0")
    (cmd,) = _builds(recorder)
    assert "BOBI_VERSION=0.38.0" in cmd


def test_source_checkout_can_force_pypi_mode(tmp_path, recorder, monkeypatch):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    monkeypatch.setattr(B, "_bobi_version", lambda: "9.9.9")
    B.build_team_image(str(repo / "agents" / "eng-team"), project_path=repo,
                       build_mode="pypi")
    (cmd,) = _builds(recorder)
    assert "BOBI_BUILD=pypi" in cmd and "BOBI_VERSION=9.9.9" in cmd
    assert cmd[-1] == str(repo)  # context unchanged, mode only flips args


def test_wheel_mode_requires_exactly_one_wheel(tmp_path, recorder):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    team = str(repo / "agents" / "eng-team")
    with pytest.raises(B.BuildError, match="exactly one"):
        B.build_team_image(team, project_path=repo, build_mode="wheel")
    (repo / "dist").mkdir()
    (repo / "dist" / "bobi-1.0-py3-none-any.whl").write_text("")
    B.build_team_image(team, project_path=repo, build_mode="wheel")
    (cmd,) = _builds(recorder)
    assert "BOBI_BUILD=wheel" in cmd
    # a stale second wheel must not silently ship (Dockerfile installs dist/*)
    (repo / "dist" / "bobi-0.9-py3-none-any.whl").write_text("")
    with pytest.raises(B.BuildError, match="exactly one"):
        B.build_team_image(team, project_path=repo, build_mode="wheel")


# --- preflight -----------------------------------------------------------------

def test_docker_missing_is_a_clear_error(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, BAKING_TEAM)
    monkeypatch.setattr(B.shutil, "which", lambda name: None)
    with pytest.raises(B.BuildError, match="docker"):
        B.build_team_image(str(repo / "agents" / "eng-team"), project_path=repo)


def test_unresolvable_team_is_a_build_error(tmp_path, recorder):
    with pytest.raises(B.BuildError):
        B.build_team_image(str(tmp_path / "nope"), project_path=tmp_path)


# --- CLI wiring ------------------------------------------------------------------

def test_cli_build_wiring(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from bobi.cli import main as cli_main

    seen = {}

    def fake_build(team, *, tags, push, build_mode, bobi_version, brains):
        seen.update(team=team, tags=tags, push=push, build_mode=build_mode,
                    bobi_version=bobi_version, brains=brains)
        return B.BuildResult(tags=tags or ["bobi-x:latest"],
                             team_dir=Path("/t"), mode="source",
                             team_deps="dist/team-deps/x.sh")

    monkeypatch.setattr("bobi.build.build_team_image", fake_build)
    result = CliRunner().invoke(cli_main, [
        "build", "./my-team", "--tag", "ghcr.io/a/t:1", "--push",
        "--build", "pypi", "--bobi-version", "0.38.0",
        "--brains", "claude,codex"])
    assert result.exit_code == 0, result.output
    assert seen == {"team": "./my-team", "tags": ["ghcr.io/a/t:1"],
                    "push": True, "build_mode": "pypi",
                    "bobi_version": "0.38.0", "brains": ["claude", "codex"]}
    assert "Built + pushed ghcr.io/a/t:1" in result.output


def test_cli_build_maps_errors(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from bobi.cli import main as cli_main

    def boom(*a, **k):
        raise B.BuildError("docker not found")

    monkeypatch.setattr("bobi.build.build_team_image", boom)
    result = CliRunner().invoke(cli_main, ["build", "./my-team"])
    assert result.exit_code != 0
    assert "docker not found" in result.output


def test_cli_build_maps_compose_and_deploy_errors(tmp_path, monkeypatch):
    """ComposeError (bad from:/catalog ref) must surface as a clean click
    error, not a traceback."""
    from click.testing import CliRunner

    from bobi.cli import main as cli_main
    from bobi.compose import ComposeError

    def boom(*a, **k):
        raise ComposeError("unknown base 'nope' in from: chain")

    monkeypatch.setattr("bobi.build.build_team_image", boom)
    result = CliRunner().invoke(cli_main, ["build", "./my-team"])
    assert result.exit_code != 0
    assert "unknown base" in result.output


# --- repo + build-asset resolution (moved with the helpers from deploy.py) ----

def test_find_repo_root_walks_up(tmp_path):
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    deep = repo / "agents" / "eng-team"
    assert B.find_repo_root(deep) == repo


def test_find_repo_root_raises_without_scripts(tmp_path):
    with pytest.raises(B.BuildError, match="not a bobi checkout"):
        B.find_repo_root(tmp_path)


def test_resolve_assets_source_mode_in_a_checkout(tmp_path):
    """In a checkout, build from source (repo Dockerfile)."""
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    a = B.resolve_assets(repo, tmp_path)
    assert a.mode == "source"
    assert a.build_context == repo
    assert a.dockerfile == repo / "Dockerfile"
    assert a.build_args == {}
    assert a.provision_sh == repo / "scripts" / "provision-instance.sh"


def test_resolve_assets_binary_mode_from_packaged(tmp_path, monkeypatch):
    """With no checkout, build from the bundled wheel assets (PyPI image)."""
    # A fake packaged _deploy dir (what the wheel ships).
    pkg = tmp_path / "_deploy"
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "docker").mkdir()
    (pkg / "Dockerfile").write_text("FROM scratch\n")
    (pkg / "docker" / "docker-entrypoint.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "provision-instance.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "destroy-instance.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(B, "find_repo_root",
                        lambda p=None: (_ for _ in ()).throw(B.BuildError("x")))
    monkeypatch.setattr(B, "_packaged_deploy_dir", lambda: pkg)
    monkeypatch.setattr(B, "_bobi_version", lambda: "9.9.9")

    staging = tmp_path / "staging"
    staging.mkdir()
    a = B.resolve_assets(tmp_path / "elsewhere", staging)
    assert a.mode == "binary"
    assert a.build_args == {"BOBI_BUILD": "pypi", "BOBI_VERSION": "9.9.9"}
    # build context assembled: Dockerfile + docker/ copied into staging
    assert (a.build_context / "Dockerfile").exists()
    assert (a.build_context / "docker" / "docker-entrypoint.sh").exists()
    assert a.provision_sh == pkg / "scripts" / "provision-instance.sh"


def test_local_package_dir_requires_agent_yaml(tmp_path):
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    with pytest.raises(B.BuildError, match="not found"):
        B.local_package_dir(repo, "nope")


def test_local_package_dir_accepts_a_path(tmp_path):
    repo = _make_repo(tmp_path, GENERIC_TEAM)
    team = tmp_path / "somewhere" / "myteam"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text("agent: myteam\n")
    assert B.local_package_dir(repo, str(team)) == team.resolve()

