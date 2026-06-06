"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

from modastack.config import ProjectConfig, LocalConfig


def test_project_config_new_path(tmp_path):
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

    config = ProjectConfig.from_file(tmp_path)

    assert config.path == tmp_path
    assert config.task_tracking == "github-issues"
    assert config.github_repo == "myorg/myrepo"
    assert config.max_parallel == 3
    assert config.test_command == "pytest -x"
    assert config.context["github_org"] == "myorg"


def test_project_config_linear(tmp_path):
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

    config = ProjectConfig.from_file(tmp_path)

    assert config.task_tracking == "linear"
    assert config.linear_team == "MOD"
    assert config.linear_project == "Baohua"
    assert config.github_repo == "myorg/myrepo"




def test_project_config_defaults(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("task_tracking:\n  system: github-issues\n")

    config = ProjectConfig.from_file(tmp_path)

    assert config.task_tracking == "github-issues"
    assert config.max_parallel == 2
    assert config.github_repo == ""
    assert config.linear_team == ""
    assert config.linear_project == ""
    assert config.context == {}


def test_project_config_missing_file(tmp_path):
    try:
        ProjectConfig.from_file(tmp_path)
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


# --- LocalConfig ---

def test_local_config_load(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "local.yaml").write_text(dedent("""
        event_server:
          deployment_id: abc
          api_key: moda_test
        dashboard_port: 9000
    """))

    local = LocalConfig.load(tmp_path)

    assert local.event_server_deployment_id == "abc"
    assert local.event_server_api_key == "moda_test"
    assert local.dashboard_port == 9000


def test_local_config_defaults_when_missing(tmp_path):
    local = LocalConfig.load(tmp_path)

    assert local.event_server_deployment_id == ""
    assert local.dashboard_port == 8095


def test_project_config_event_server(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(dedent("""
        task_tracking:
          system: github-issues
        event_server:
          url: https://modastack-events.example.com
    """))

    config = ProjectConfig.from_file(tmp_path)
    assert config.event_server_url == "https://modastack-events.example.com"


def test_local_config_save_roundtrip(tmp_path):
    config_dir = tmp_path / ".modastack"
    config_dir.mkdir()

    local = LocalConfig(
        event_server_deployment_id="abc",
        event_server_api_key="moda_test",
    )
    local.save(tmp_path)

    loaded = LocalConfig.load(tmp_path)
    assert loaded.event_server_deployment_id == "abc"
    assert loaded.event_server_api_key == "moda_test"
