"""Tests for the team-deps hook renderer (C24 team-flavored images)."""

from textwrap import dedent

import pytest

from modastack import build_render
from modastack.build_render import (
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


def test_renders_apt_npm_run_verify(tmp_path):
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    # apt as root (no gosu)
    assert "apt-get install -y --no-install-recommends nodejs npm" in script
    # npm global
    assert "npm install -g @openai/codex" in script
    # run step in the IMAGE home, as the modastack user, with CLAUDE_CONFIG_DIR
    # stripped (so skills bake to the image ~/.claude, not the runtime volume).
    assert f"gosu modastack env -u CLAUDE_CONFIG_DIR HOME={TEAM_HOME} bash -lc" in script
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
    assert run_line.startswith("gosu modastack")  # run drops to the user


def test_no_seed_or_stamp_machinery(tmp_path):
    # The image-home model copies nothing onto the volume — there is no seed
    # dir, no tool stamp, and no /opt/modastack/home-seed anywhere.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert ".modastack-tool-stamp" not in script
    assert "home-seed" not in script


def test_skills_baked_outside_dotclaude(tmp_path):
    # Run-step teams bake skills at BAKED_SKILLS (outside ~/.claude) via a
    # build-time ~/.claude/skills symlink, so the entrypoint can later point the
    # whole ~/.claude at the volume without clobbering skills.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    assert f"install -d -o modastack -g modastack {BAKED_SKILLS}" in script
    assert f"ln -sfn {BAKED_SKILLS} ~/.claude/skills" in script
    # ordering: the bake-setup precedes the run step that writes into ~/.claude/skills
    assert script.index(BAKED_SKILLS) < script.index("git clone")


def test_verify_uses_same_home_as_run(tmp_path):
    # The whole point of the redesign: build-time `verify` reads the SAME HOME
    # the `run` steps wrote and the runtime agent uses — no build/runtime gap.
    script = render_team_deps_script(_team(tmp_path, ENG_TEAM))
    prefix = f"gosu modastack env -u CLAUDE_CONFIG_DIR HOME={TEAM_HOME} bash -lc"
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
    assert ".modastack-tool-stamp" not in script
    assert "install -d" not in script  # no seed dir created


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


def test_main_check_exit_codes(tmp_path):
    (tmp_path / "agent.yaml").write_text("agent: t\nbuild:\n  apt: [git]\n")
    assert build_render._main([str(tmp_path), "--check"]) == 0
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "agent.yaml").write_text("agent: t\n")
    assert build_render._main([str(bare), "--check"]) == 2
