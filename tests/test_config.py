"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

from modastack.config import RepoConfig, GlobalConfig


def test_repo_config_new_path(tmp_path):
    """Config loads from .modastack/config.yaml."""
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        task_tracking:
          system: "github-issues"
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
    assert config.task_tracking == "github-issues"
    assert config.project == "MYPROJ"
    assert config.trigger_labels == ["agent", "auto"]
    assert config.skip_labels == ["blocked"]
    assert config.max_parallel == 3
    assert config.test_command == "pytest -x"
    assert config.review_required is False
    assert config.auto_merge is True
    assert config.credentials == "myproject"
    assert config.context["github_org"] == "myorg"


def test_repo_config_legacy_path(tmp_path):
    """Config still loads from .modastack.yaml (backward compat)."""
    import warnings
    config_file = tmp_path / ".modastack.yaml"
    config_file.write_text(dedent("""
        task_tracking:
          system: "github-issues"
          project: "LEGACY"
    """))

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = RepoConfig.from_file(tmp_path)
        assert config.project == "LEGACY"
        assert any("deprecated" in str(warning.message).lower() for warning in w)


def test_repo_config_new_path_preferred(tmp_path):
    """New path wins over legacy when both exist."""
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("task_tracking:\n  project: NEW\n")
    (tmp_path / ".modastack.yaml").write_text("task_tracking:\n  project: OLD\n")

    config = RepoConfig.from_file(tmp_path)
    assert config.project == "NEW"


def test_repo_config_backwards_compat_linear(tmp_path):
    """Old configs with 'linear:' section still work."""
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        linear:
          project: "BET"
          trigger_labels: ["agent", "auto"]
          skip_labels: ["blocked"]

        agent:
          max_parallel: 3

        verify:
          test_command: "pytest -x"
          review_required: false
          auto_merge: true

        credentials: myproject
    """))

    config = RepoConfig.from_file(tmp_path)

    assert config.task_tracking == "linear"
    assert config.project == "BET"
    assert config.linear_project == "BET"
    assert config.trigger_labels == ["agent", "auto"]


def test_repo_config_defaults(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("task_tracking:\n  project: X\n")

    config = RepoConfig.from_file(tmp_path)

    assert config.task_tracking == "github-issues"
    assert config.project == "X"
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
        repos=[Path("/tmp/repo1"), Path("/tmp/repo2")],
        github_default_account="testuser",
    )
    config.save()

    loaded = GlobalConfig.load()
    assert loaded.slack_bot_token == "xoxb-test"
    assert len(loaded.repos) == 2
    assert loaded.github_default_account == "testuser"
