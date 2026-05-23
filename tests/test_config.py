"""Tests for config loading."""

from pathlib import Path
from textwrap import dedent

import yaml

from modastack.config import RepoEntry, GlobalConfig


def test_repo_entry_from_dict():
    entry = RepoEntry(
        path=Path("/tmp/repo"),
        remote="moda-labs/bettertab",
        linear_project="BT",
        credentials="myproject",
        trigger_labels=["agent", "auto"],
        skip_labels=["blocked"],
    )
    assert entry.path == Path("/tmp/repo")
    assert entry.remote == "moda-labs/bettertab"
    assert entry.linear_project == "BT"
    assert entry.credentials == "myproject"
    assert entry.trigger_labels == ["agent", "auto"]
    assert entry.skip_labels == ["blocked"]


def test_repo_entry_defaults():
    entry = RepoEntry(path=Path("/tmp/repo"))
    assert entry.remote == ""
    assert entry.linear_project == ""
    assert entry.credentials == "default"
    assert entry.trigger_labels == ["agent"]
    assert entry.skip_labels == ["blocked", "human-only"]


def test_global_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.yaml")
    config = GlobalConfig.load()
    assert config.slack_bot_token == ""
    assert config.repos == []


def test_global_config_repos_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    config = GlobalConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        repos=[
            RepoEntry(
                path=Path("/tmp/repo1"),
                remote="moda-labs/repo1",
                linear_project="R1",
                credentials="proj1",
            ),
            RepoEntry(
                path=Path("/tmp/repo2"),
                linear_project="R2",
            ),
        ],
        github_default_account="testuser",
    )
    config.save()

    loaded = GlobalConfig.load()
    assert loaded.slack_bot_token == "xoxb-test"
    assert loaded.slack_app_token == "xapp-test"
    assert len(loaded.repos) == 2
    assert loaded.github_default_account == "testuser"

    r1 = loaded.repos[0]
    assert r1.path == Path("/tmp/repo1")
    assert r1.remote == "moda-labs/repo1"
    assert r1.linear_project == "R1"
    assert r1.credentials == "proj1"

    r2 = loaded.repos[1]
    assert r2.path == Path("/tmp/repo2")
    assert r2.remote == ""
    assert r2.linear_project == "R2"
    assert r2.credentials == "default"


def test_repo_paths_property():
    config = GlobalConfig(repos=[
        RepoEntry(path=Path("/a")),
        RepoEntry(path=Path("/b")),
    ])
    assert config.repo_paths == [Path("/a"), Path("/b")]


def test_get_repo_found(tmp_path):
    entry = RepoEntry(path=tmp_path, linear_project="TEST")
    config = GlobalConfig(repos=[entry])
    found = config.get_repo(tmp_path)
    assert found is not None
    assert found.linear_project == "TEST"


def test_get_repo_not_found(tmp_path):
    config = GlobalConfig(repos=[
        RepoEntry(path=tmp_path, linear_project="TEST"),
    ])
    assert config.get_repo(Path("/nonexistent")) is None


def test_save_omits_default_values(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    config = GlobalConfig(repos=[
        RepoEntry(path=Path("/tmp/repo"), linear_project="X"),
    ])
    config.save()

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    repo = raw["repos"][0]
    assert "remote" not in repo
    assert "credentials" not in repo
    assert "trigger_labels" not in repo
    assert "skip_labels" not in repo
    assert repo["path"] == "/tmp/repo"
    assert repo["linear_project"] == "X"


def test_load_string_entries_still_work(tmp_path, monkeypatch):
    """String entries in repos list are parsed as RepoEntry with path only."""
    monkeypatch.setattr("modastack.config.GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(yaml.dump({
        "repos": ["/tmp/old-style-repo"],
    }))
    config = GlobalConfig.load()
    assert len(config.repos) == 1
    assert config.repos[0].path == Path("/tmp/old-style-repo")
    assert config.repos[0].remote == ""
