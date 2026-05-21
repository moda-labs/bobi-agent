"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

from modastack.config import RepoConfig, GlobalConfig


def test_repo_config_from_file(tmp_path):
    config_file = tmp_path / ".modastack.yaml"
    config_file.write_text(dedent("""
        linear:
          project: "MYPROJ"
          trigger_labels: ["agent", "auto"]
          skip_labels: ["blocked"]

        agent:
          max_parallel: 3

        verify:
          test_command: "pytest -x"
          review_required: false
          auto_merge: true

        credentials: myproject

        context:
          github_org: myorg
          notes: "test repo"
    """))

    config = RepoConfig.from_file(tmp_path)

    assert config.path == tmp_path
    assert config.linear_project == "MYPROJ"
    assert config.trigger_labels == ["agent", "auto"]
    assert config.skip_labels == ["blocked"]
    assert config.max_parallel == 3
    assert config.test_command == "pytest -x"
    assert config.review_required is False
    assert config.auto_merge is True
    assert config.credentials == "myproject"
    assert config.context["github_org"] == "myorg"


def test_repo_config_defaults(tmp_path):
    config_file = tmp_path / ".modastack.yaml"
    config_file.write_text("linear:\n  project: X\n")

    config = RepoConfig.from_file(tmp_path)

    assert config.linear_project == "X"
    assert config.trigger_labels == ["agent"]
    assert config.skip_labels == ["blocked", "human-only"]
    assert config.max_parallel == 2
    assert config.review_required is True
    assert config.auto_merge is False
    assert config.context == {}


def test_repo_config_missing_file(tmp_path):
    try:
        RepoConfig.from_file(tmp_path)
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


def test_global_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.yaml")
    config = GlobalConfig.load()
    assert config.slack_bot_token == ""
    assert config.repos == []


def test_global_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    config = GlobalConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        repos=[Path("/tmp/repo1"), Path("/tmp/repo2")],
        github_default_account="testuser",
    )
    config.save()

    loaded = GlobalConfig.load()
    assert loaded.slack_bot_token == "xoxb-test"
    assert loaded.slack_app_token == "xapp-test"
    assert len(loaded.repos) == 2
    assert loaded.github_default_account == "testuser"
