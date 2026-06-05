"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

from modastack.config import RepoConfig, LocalConfig


def test_repo_config_new_path(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        task_tracking:
          system: "github-issues"
        github:
          repo: "myorg/myrepo"
        agent:
          max_parallel: 3
        verify:
          test_command: "pytest -x"
        context:
          github_org: myorg
    """))

    config = RepoConfig.from_file(tmp_path)

    assert config.path == tmp_path
    assert config.task_tracking == "github-issues"
    assert config.github_repo == "myorg/myrepo"
    assert config.max_parallel == 3
    assert config.test_command == "pytest -x"
    assert config.context["github_org"] == "myorg"


def test_repo_config_linear(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        task_tracking:
          system: linear
        linear:
          team: MOD
          project: Baohua
        github:
          repo: myorg/myrepo
    """))

    config = RepoConfig.from_file(tmp_path)

    assert config.task_tracking == "linear"
    assert config.linear_team == "MOD"
    assert config.linear_project == "Baohua"
    assert config.github_repo == "myorg/myrepo"


def test_repo_config_legacy_path(tmp_path):
    import warnings
    config_file = tmp_path / ".modastack.yaml"
    config_file.write_text(dedent("""
        task_tracking:
          system: "github-issues"
    """))

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        config = RepoConfig.from_file(tmp_path)
        assert config.task_tracking == "github-issues"
        assert any("deprecated" in str(warning.message).lower() for warning in w)


def test_repo_config_new_path_preferred(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("task_tracking:\n  system: github-issues\n")
    (tmp_path / ".modastack.yaml").write_text("task_tracking:\n  system: linear\n")

    config = RepoConfig.from_file(tmp_path)
    assert config.task_tracking == "github-issues"


def test_repo_config_defaults(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("task_tracking:\n  system: github-issues\n")

    config = RepoConfig.from_file(tmp_path)

    assert config.task_tracking == "github-issues"
    assert config.max_parallel == 2
    assert config.github_repo == ""
    assert config.linear_team == ""
    assert config.linear_project == ""
    assert config.context == {}


def test_repo_config_missing_file(tmp_path):
    try:
        RepoConfig.from_file(tmp_path)
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


# --- LocalConfig ---

def test_local_config_load(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(dedent("""
        operator:
          name: test
          email: test@test.com
        slack:
          bot_token: xoxb-test
          dm_channel: D123
        event_server:
          url: http://localhost:8080
          deployment_id: abc
          api_key: moda_test
        dashboard_port: 9000
    """))

    local = LocalConfig.load(tmp_path)

    assert local.operator_name == "test"
    assert local.operator_email == "test@test.com"
    assert local.slack_bot_token == "xoxb-test"
    assert local.slack_dm_channel == "D123"
    assert local.event_server_url == "http://localhost:8080"
    assert local.dashboard_port == 9000


def test_local_config_defaults_when_missing(tmp_path):
    local = LocalConfig.load(tmp_path)

    assert local.operator_name == ""
    assert local.slack_bot_token == ""
    assert local.event_server_url == ""
    assert local.dashboard_port == 8095


def test_local_config_save_roundtrip(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()

    local = LocalConfig(
        operator_name="test",
        slack_bot_token="xoxb-test",
        slack_dm_channel="D123",
    )
    local.save(tmp_path)

    loaded = LocalConfig.load(tmp_path)
    assert loaded.operator_name == "test"
    assert loaded.slack_bot_token == "xoxb-test"
