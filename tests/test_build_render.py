"""Tests for the team-deps hook renderer (C24 team-flavored images)."""

from textwrap import dedent

import pytest

from bobi import build_render
from bobi.build_render import (
    BAKED_SKILLS,
    TEAM_HOME,
    load_team_config,
    render_team_deps_script,
    team_deps_hash,
)


def _team(tmp_path, body):
    (tmp_path / "agent.yaml").write_text(dedent(body))
    return load_team_config(tmp_path)


ENG_TEAM = """
    agent: eng-team
    build:
      apt: [nodejs, npm]
      npm: ["@openai/codex"]
      run:
        - "git clone https://x/gstack ~/dev/gstack && cd ~/dev/gstack && ./setup"
      verify: requires
    requires:
      - name: gstack
        check: "test -e ~/.claude/skills/browse/SKILL.md"
      - name: codex
        check: "command -v codex"
"""


def test_composed_loader_bakes_tool_library_cli(tmp_path):
    """A team that declares its CLI via `tool_library:` (#416) instead of an
    inline `build:` must STILL bake that CLI into the deploy image.

    Regression for the personal-assistant/venn deploy bug: the team-deps renderer
    read the RAW leaf agent.yaml, which has no `build:` after migrating venn to
    `tool_library: [venn]`, so venn was never baked. On the box the dispatch-time
    `requires: venn` gate then failed and blocked EVERY agent (no replies). The
    renderer must read the COMPOSED build, which includes tool_library expansion.
    """
    team = tmp_path / "agents" / "pa"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text(dedent("""
        agent: pa
        tool_library:
          - venn
    """))
    # The raw load sees no build: — this is the latent bug surface.
    assert load_team_config(team).build is None
    # The composed load expands the catalog venn entry into a real build: spec.
    cfg = build_render.load_composed_team_config(team, tmp_path)
    assert cfg.build is not None
    script = render_team_deps_script(cfg)
    assert "venn-cli==0.2.0" in script   # the pin from the catalog entry
    assert "/opt/venn-cli" in script     # the isolated venv install


def test_codex_brain_bakes_nothing(tmp_path):
    """A Codex-brained team bakes nothing: the Codex CLI ships in the base image
    (#428), so `brain: codex` no longer implies a codex dependency/build."""
    team = tmp_path / "agents" / "codex-team"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text(dedent("""
        agent: codex-team
        brain:
          kind: codex
    """))
    assert load_team_config(team).build is None
    # Composed, too: no implied codex build to render.
    assert build_render.load_composed_team_config(team, tmp_path).build is None


def test_cli_renders_composed_build_for_standalone_tool_library(tmp_path):
    """The `bobi.build_render` CLI (what build-team-images.sh runs in CI) must
    render the COMPOSED build, so a standalone team declaring its CLI via
    `tool_library:` (no inline build:) is baked by the CI gate exactly as
    `bobi deploy` bakes it - not silently skipped. Regression for the
    CI-vs-deploy divergence that left codex-test hand-declaring its build (#428).
    """
    team = tmp_path / "agents" / "pa"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text(dedent("""
        agent: pa
        tool_library:
          - venn
    """))
    # Raw read sees no build: the pre-#428 CLI behavior that skipped the team.
    assert load_team_config(team).build is None
    # --check now reports a build (exit 0) because the CLI composes.
    assert build_render._main([str(team), "--check"]) == 0
    out = tmp_path / "pa.sh"
    assert build_render._main([str(team), "--out", str(out)]) == 0
    script = out.read_text()
    assert "venn-cli==0.2.0" in script      # catalog pin, baked via expansion
    assert "/opt/venn-cli" in script


def test_renders_apt_npm_run_verify(tmp_path):
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    # apt as root (no gosu)
    assert "apt-get install -y --no-install-recommends nodejs npm" in script
    # npm global
    assert "npm install -g @openai/codex" in script
    # run step in the IMAGE home, as the bobi user, with CLAUDE_CONFIG_DIR
    # stripped (so skills bake to the image ~/.claude, not the runtime volume).
    assert f"gosu bobi env -u CLAUDE_CONFIG_DIR HOME={TEAM_HOME} bash -lc" in script
    assert "git clone https://x/gstack ~/dev/gstack" in script
    # verify re-runs each requires.check
    assert "test -e ~/.claude/skills/browse/SKILL.md" in script
    assert "command -v codex" in script


def test_run_steps_run_as_user_apt_as_root(tmp_path):
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    lines = script.splitlines()
    apt_line = next(ln for ln in lines if "apt-get install" in ln)
    run_line = next(ln for ln in lines if "git clone" in ln and ln.startswith("gosu"))
    assert "gosu" not in apt_line          # apt is root
    assert run_line.startswith("gosu bobi")  # run drops to the user


def test_no_seed_or_stamp_machinery(tmp_path):
    # The image-home model copies nothing onto the volume — there is no seed
    # dir, no tool stamp, and no /opt/bobi/home-seed anywhere.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert ".bobi-tool-stamp" not in script
    assert "home-seed" not in script


def test_skills_baked_outside_dotclaude(tmp_path):
    # Run-step teams bake skills at BAKED_SKILLS (outside ~/.claude) via a
    # build-time ~/.claude/skills symlink, so the entrypoint can later point the
    # whole ~/.claude at the volume without clobbering skills.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert f"install -d -o bobi -g bobi {BAKED_SKILLS}" in script
    assert f"ln -sfn {BAKED_SKILLS} ~/.claude/skills" in script
    # ordering: the bake-setup precedes the run step that writes into ~/.claude/skills
    assert script.index(BAKED_SKILLS) < script.index("git clone")


def test_verify_uses_same_home_as_run(tmp_path):
    # The whole point of the redesign: build-time `verify` reads the SAME HOME
    # the `run` steps wrote and the runtime agent uses — no build/runtime gap.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    prefix = f"gosu bobi env -u CLAUDE_CONFIG_DIR HOME={TEAM_HOME} bash -lc"
    run_line = next(ln for ln in script.splitlines()
                    if "git clone" in ln and ln.startswith("gosu"))
    verify_line = next(ln for ln in script.splitlines()
                       if "test -e ~/.claude/skills/browse" in ln)
    assert run_line.startswith(prefix)
    assert verify_line.startswith(prefix)


def test_hash_is_stable_and_input_sensitive(tmp_path):
    cfg = _team(tmp_path, ENG_TEAM)
    h1 = team_deps_hash(cfg.build)
    h2 = team_deps_hash(cfg.build)
    assert h1 == h2 and len(h1) == 12
    cfg.build.npm = ["@openai/codex", "something-else"]
    assert team_deps_hash(cfg.build) != h1


def test_apt_only_team_has_no_seed_or_stamp(tmp_path):
    # codex-style: npm global only, no ~-writing run steps → nothing to seed.
    script = render_team_deps_script(_team(tmp_path, """
        agent: t
        build:
          apt: [nodejs, npm]
          npm: ["@openai/codex"]
    """))
    assert ".bobi-tool-stamp" not in script
    assert "install -d" not in script  # no seed dir created (deps-identity stamp uses mkdir -p)


def test_renders_deps_identity_stamp(tmp_path):
    # Every team-deps hook stamps its deps hash into the image (#379) so a running
    # instance can report what tools it was built with — deploy reads it to detect
    # a `build:` drift before the silent hot-push path.
    cfg = _team(tmp_path, ENG_TEAM)
    script = render_team_deps_script(cfg)
    assert f"> {build_render.TEAM_DEPS_STAMP}" in script
    assert team_deps_hash(cfg.build) in script
    # uses mkdir -p, never install -d (which would mark a seed-dir team)
    assert "mkdir -p /opt/bobi" in script


def test_deps_stamp_moves_with_the_spec(tmp_path):
    # The stamped value IS the cache key, so bumping a dep changes both together.
    a = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    b = render_team_deps_script(_team(tmp_path, ENG_TEAM.replace(
        '"@openai/codex"', '"@openai/codex", "extra-pkg"')))
    assert a != b


def test_run_root_runs_as_root_before_user_steps(tmp_path):
    script = render_team_deps_script(_team(tmp_path, """
        agent: t
        build:
          npm: [bun]
          run_root:
            - "npx --yes playwright install-deps chromium"
          run:
            - "git clone x ~/dev/gstack && ./setup"
    """))
    lines = script.splitlines()
    root_line = next(ln for ln in lines if "playwright install-deps" in ln and not ln.startswith("echo"))
    user_line = next(ln for ln in lines if "git clone" in ln and ln.startswith("gosu"))
    assert not root_line.startswith("gosu")  # run_root is root
    assert lines.index(root_line) < lines.index(user_line)  # before the user run


def test_run_root_alone_is_a_valid_build(tmp_path):
    # run_root with nothing else still renders (a root-only setup).
    script = render_team_deps_script(_team(tmp_path, """
        agent: t
        build:
          run_root: ["echo hi"]
    """))
    assert "echo hi" in script


def test_render_rejects_no_build(tmp_path):
    cfg = _team(tmp_path, "agent: t\n")
    with pytest.raises(ValueError):
        render_team_deps_script(cfg)


def test_render_rejects_pure_dockerfile_escape_hatch(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    cfg = _team(tmp_path, "agent: t\n")
    assert cfg.build is not None and cfg.build.dockerfile
    with pytest.raises(ValueError):
        render_team_deps_script(cfg)


# --- #428 Stage 3: resolved recipes + dep-list stamp ------------------------


def test_extra_recipes_bake_through_the_one_renderer(tmp_path):
    # A guide-only dep's agent-resolved recipe re-enters the SAME renderer as an
    # inline build:, appended after the declarative steps and de-duped.
    cfg = _team(tmp_path, """
        agent: t
        build:
          apt: [git]
          npm: ["pinned@1.0.0"]
    """)
    script = render_team_deps_script(cfg, extra_recipes=[
        {"npm": ["gstack@1.2.3"], "run": ["gstack init"]},
        {"apt": ["git", "curl"]},  # 'git' de-dupes against the declarative apt
    ])
    assert "npm install -g pinned@1.0.0 gstack@1.2.3" in script
    assert "apt-get install -y --no-install-recommends git curl" in script
    assert "gstack init" in script


def test_extra_recipes_do_not_move_the_379_stamp(tmp_path):
    # The #379 stamp is the DECLARATIVE identity — a (non-deterministic) resolved
    # recipe must not churn it, or deploy (which never runs the agent) would never
    # match the running image.
    cfg = _team(tmp_path, "agent: t\nbuild:\n  apt: [git]\n")
    plain = render_team_deps_script(cfg)
    with_recipe = render_team_deps_script(
        cfg, extra_recipes=[{"npm": ["resolved@9.9.9"]}])
    stamp = team_deps_hash(cfg.build)
    assert f"printf '%s\\n' {stamp} > {build_render.TEAM_DEPS_STAMP}" in plain
    assert f"printf '%s\\n' {stamp} > {build_render.TEAM_DEPS_STAMP}" in with_recipe
    assert "resolved@9.9.9" in with_recipe and "resolved@9.9.9" not in plain


def test_guide_only_team_renders_from_recipes_alone(tmp_path):
    # A guide-only team has NO build: spec (no pinned install); the resolved recipe
    # is its whole build. verify: requires still re-runs the team's checks.
    cfg = _team(tmp_path, """
        agent: t
        requires:
          - name: gstack
            check: "gstack --version"
    """)
    assert cfg.build is None
    script = render_team_deps_script(
        cfg, extra_recipes=[{"npm": ["gstack@1.2.3"]}],
        dep_list_hash="abc123def456")
    assert "npm install -g gstack@1.2.3" in script
    assert "gstack --version" in script  # verify re-run
    assert f"abc123def456 > {build_render.DEP_LIST_STAMP}" in script


def test_dep_list_hash_stamp_is_opt_in(tmp_path):
    # A bare render (pre-Stage-3 callers) stamps only the #379 hash, byte-for-byte.
    cfg = _team(tmp_path, "agent: t\nbuild:\n  apt: [git]\n")
    assert build_render.DEP_LIST_STAMP not in render_team_deps_script(cfg)
    withhash = render_team_deps_script(cfg, dep_list_hash="deadbeef")
    assert f"deadbeef > {build_render.DEP_LIST_STAMP}" in withhash


def test_render_still_rejects_empty_when_no_recipes(tmp_path):
    cfg = _team(tmp_path, "agent: t\n")
    with pytest.raises(ValueError):
        render_team_deps_script(cfg, extra_recipes=[])


def test_main_check_exit_codes(tmp_path):
    (tmp_path / "agent.yaml").write_text("agent: t\nbuild:\n  apt: [git]\n")
    assert build_render._main([str(tmp_path), "--check"]) == 0
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "agent.yaml").write_text("agent: t\n")
    assert build_render._main([str(bare), "--check"]) == 2
