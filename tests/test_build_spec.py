"""Tests for the `build:` spec parsing (C24 team-flavored images)."""

from textwrap import dedent

from bobi.config import BuildSpec, Config


def _write_agent_yaml(tmp_path, body):
    d = tmp_path / ".bobi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(dedent(body))


def test_no_build_block_is_none(tmp_path):
    _write_agent_yaml(tmp_path, "agent: t\n")
    assert Config.load(tmp_path).build is None


def test_full_build_block_parses(tmp_path):
    _write_agent_yaml(tmp_path, """
        agent: eng-team
        build:
          apt: [nodejs, npm]
          npm: ["@openai/codex"]
          run:
            - "git clone x ~/dev/gstack && cd ~/dev/gstack && ./setup"
          verify: requires
    """)
    b = Config.load(tmp_path).build
    assert isinstance(b, BuildSpec)
    assert b.apt == ["nodejs", "npm"]
    assert b.npm == ["@openai/codex"]
    assert b.run == ["git clone x ~/dev/gstack && cd ~/dev/gstack && ./setup"]
    assert b.verify_requires is True
    assert b.dockerfile == ""


def test_build_steps_not_env_interpolated(tmp_path, monkeypatch):
    # build commands are shell, run at image-build time — a literal ${VAR}
    # must survive parse verbatim, not get resolved from the host env.
    monkeypatch.setenv("FOO", "leaked")
    _write_agent_yaml(tmp_path, """
        agent: t
        build:
          run:
            - "echo ${FOO}"
    """)
    assert Config.load(tmp_path).build.run == ["echo ${FOO}"]


def test_scalar_fields_coerce_to_lists(tmp_path):
    _write_agent_yaml(tmp_path, """
        agent: t
        build:
          apt: git
          run: "./setup"
    """)
    b = Config.load(tmp_path).build
    assert b.apt == ["git"]
    assert b.run == ["./setup"]


def test_empty_build_block_is_none(tmp_path):
    # A `build:` with nothing actionable deploys on the generic base.
    _write_agent_yaml(tmp_path, "agent: t\nbuild: {}\n")
    assert Config.load(tmp_path).build is None


def test_verify_only_block_is_kept(tmp_path):
    # `verify: requires` alone is meaningful (a build-time gate), so keep it.
    _write_agent_yaml(tmp_path, """
        agent: t
        build:
          verify: requires
    """)
    b = Config.load(tmp_path).build
    assert b is not None and b.verify_requires is True


def test_sibling_dockerfile_escape_hatch(tmp_path):
    # A raw Dockerfile next to agent.yaml counts as a build even with no block.
    d = tmp_path / ".bobi"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text("agent: t\n")
    (d / "Dockerfile").write_text("FROM ghcr.io/moda-labs/bobi-base\n")
    b = Config.load(tmp_path).build
    assert b is not None
    assert b.dockerfile.endswith("Dockerfile")
    assert b.is_empty is False  # the Dockerfile makes it non-empty
