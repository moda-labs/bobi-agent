"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

from dispatch.config import RepoConfig, GlobalConfig


def test_repo_config_from_file(tmp_path):
    config_file = tmp_path / ".dispatch.yaml"
    config_file.write_text(dedent("""
        linear:
          project: "MYPROJ"
          trigger_labels: ["agent", "auto"]
          skip_labels: ["blocked"]

        complexity:
          trivial: "label:typo OR label:docs"
          heavy: "label:feature OR estimate>3"

        agent:
          tool: "codex"
          skills: ["review", "ship", "qa"]
          max_parallel: 3

        verify:
          test_command: "pytest -x"
          review_required: false
          auto_merge: true

        notify:
          slack_channel: "#builds"
    """))

    config = RepoConfig.from_file(tmp_path)

    assert config.path == tmp_path
    assert config.linear_project == "MYPROJ"
    assert config.trigger_labels == ["agent", "auto"]
    assert config.skip_labels == ["blocked"]
    assert config.complexity_rules["trivial"] == "label:typo OR label:docs"
    assert config.complexity_rules["heavy"] == "label:feature OR estimate>3"
    assert config.agent_tool == "codex"
    assert config.skills == ["review", "ship", "qa"]
    assert config.max_parallel == 3
    assert config.test_command == "pytest -x"
    assert config.review_required is False
    assert config.auto_merge is True
    assert config.slack_channel == "#builds"


def test_repo_config_defaults(tmp_path):
    config_file = tmp_path / ".dispatch.yaml"
    config_file.write_text("linear:\n  project: X\n")

    config = RepoConfig.from_file(tmp_path)

    assert config.linear_project == "X"
    assert config.trigger_labels == ["agent"]
    assert config.skip_labels == ["blocked", "human-only"]
    assert config.agent_tool == "claude"
    assert config.max_parallel == 2
    assert config.review_required is True
    assert config.auto_merge is False


def test_repo_config_missing_file(tmp_path):
    try:
        RepoConfig.from_file(tmp_path)
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


def test_global_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("dispatch.config.GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.yaml")
    config = GlobalConfig.load()
    assert config.linear_api_key == ""
    assert config.repos == []


def test_global_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("dispatch.config.GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("dispatch.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    config = GlobalConfig(
        linear_api_key="lin_test_123",
        slack_bot_token="xoxb-test",
        repos=[Path("/tmp/repo1"), Path("/tmp/repo2")],
        poll_interval_minutes=10,
    )
    config.save()

    loaded = GlobalConfig.load()
    assert loaded.linear_api_key == "lin_test_123"
    assert loaded.slack_bot_token == "xoxb-test"
    assert len(loaded.repos) == 2
    assert loaded.poll_interval_minutes == 10
