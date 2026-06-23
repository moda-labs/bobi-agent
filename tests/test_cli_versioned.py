"""CLI `name@version` parsing for `install` and `agents update` (#440 Phase 2).

The `@version` suffix pins an immutable per-team asset and is meaningful ONLY on
the registry-name branch — never on the URL / local-archive / local-dir branches
(D-6). These tests assert the version is split off the last `@` and threaded into
`registry.fetch(..., version=…)`.
"""

from unittest.mock import patch

from click.testing import CliRunner

from modastack import registry
from modastack.cli import main


def test_install_pins_version(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "fetch",
                        lambda pp, name, *, version=None, repo=None:
                        calls.append((name, version)))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # _resolve_agent_pack returns None → exits right after fetch; we only
        # care that the version was parsed and forwarded.
        runner.invoke(main, ["install", "eng-team@1.1.0", "--non-interactive"])
    assert calls == [("eng-team", "1.1.0")]


def test_install_bare_name_is_latest(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "fetch",
                        lambda pp, name, *, version=None, repo=None:
                        calls.append((name, version)))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["install", "eng-team", "--non-interactive"])
    assert calls == [("eng-team", None)]


def test_install_url_branch_does_not_split_on_at(tmp_path, monkeypatch):
    """A URL containing `@` must NOT be parsed as name@version — it routes to
    fetch_from_url untouched."""
    seen = {}
    monkeypatch.setattr(registry, "fetch_from_url",
                        lambda pp, url, name=None: seen.update(url=url) or (
                            __import__("pathlib").Path(pp) / "x", "x"))
    monkeypatch.setattr(registry, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("registry.fetch must not run for a URL install")))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["install",
                             "https://example.com/teams/eng@v1.tar.gz",
                             "--non-interactive"])
    assert seen.get("url") == "https://example.com/teams/eng@v1.tar.gz"


def test_agents_update_pins_version(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "fetch",
                        lambda pp, name, *, version=None, repo=None:
                        calls.append((name, version)))
    monkeypatch.setattr(registry, "_read_local_version", lambda pp, name: "1.1.0")
    # A pin must NOT consult check_update (no latest-vs-local short-circuit).
    monkeypatch.setattr(registry, "check_update", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a pin must not call check_update")))
    runner = CliRunner()
    with patch("modastack.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["agents", "update", "eng-team@1.1.0"])
    assert calls == [("eng-team", "1.1.0")]
    assert "Pinned eng-team to v1.1.0" in result.output


def test_agents_update_bare_name_checks_latest(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "check_update", lambda pp, name: ("1.0.0", "1.1.0"))
    monkeypatch.setattr(registry, "fetch",
                        lambda pp, name, *, version=None, repo=None:
                        calls.append((name, version)) or (tmp_path / name))
    monkeypatch.setattr(registry, "_read_local_version", lambda pp, name: "1.1.0")
    runner = CliRunner()
    with patch("modastack.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["agents", "update", "eng-team"])
    assert calls == [("eng-team", None)]
    assert "→ v1.1.0" in result.output
