"""Contract tests for the Bobi Agent home/runtime path model."""

import os
from pathlib import Path

import pytest

from bobi import paths


@pytest.fixture(autouse=True)
def unbound(monkeypatch):
    monkeypatch.setattr(paths, "_root", None)
    monkeypatch.delenv("BOBI_ROOT", raising=False)
    monkeypatch.delenv("BOBI_HOME", raising=False)


def _install(home: Path, name: str = "eng") -> Path:
    run = home / "agents" / name / "run"
    package = run / "package"
    (package).mkdir(parents=True)
    (package / "agent.yaml").write_text("agent: test\n")
    (run / "state").mkdir()
    (run / "workspace").mkdir()
    return run


class TestHome:
    def test_home_defaults_to_hidden_user_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert paths.home_dir() == tmp_path / ".bobi"

    def test_home_comes_only_from_env(self, monkeypatch, tmp_path):
        home = tmp_path / "custom-home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        assert paths.home_dir() == home.resolve()
        assert paths.agents_root() == home.resolve() / "agents"


class TestBindRoot:
    def test_binds_and_resolves_symlinks(self, tmp_path):
        real = _install(tmp_path / "home")
        link = tmp_path / "link"
        link.symlink_to(real)

        paths.bind_root(link)
        assert paths.bobi_root() == real
        assert os.environ["BOBI_ROOT"] == str(real)

    def test_rebind_same_path_is_noop(self, tmp_path):
        root = _install(tmp_path / "home")
        paths.bind_root(root)
        paths.bind_root(root)
        assert paths.bobi_root() == root.resolve()

    def test_rebind_different_path_raises(self, tmp_path):
        root1 = _install(tmp_path / "one", "one")
        root2 = _install(tmp_path / "two", "two")
        paths.bind_root(root1)
        with pytest.raises(RuntimeError, match="already bound"):
            paths.bind_root(root2)

    def test_none_unbinds(self, tmp_path):
        root = _install(tmp_path / "home")
        paths.bind_root(root)
        paths.bind_root(None)
        assert paths.bound_root() is None
        assert "BOBI_ROOT" not in os.environ


class TestResolveRoot:
    def test_resolve_root_for_agent_uses_bobi_home(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        run = _install(home, "eng")
        monkeypatch.setenv("BOBI_HOME", str(home))

        assert paths.resolve_root_for_agent("eng") == run.resolve()

    def test_list_agents_only_returns_installed_agents(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _install(home, "eng")
        (home / "agents" / "draft" / "src").mkdir(parents=True)
        monkeypatch.setenv("BOBI_HOME", str(home))

        assert paths.list_agents() == ["eng"]

    def test_no_cwd_walkup(self, tmp_path):
        home = tmp_path / "home"
        run = _install(home, "eng")
        deep = run / "workspace" / "repo" / "src"
        deep.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="No Bobi Agent runtime selected"):
            paths.resolve_root(deep)

    def test_explicit_runtime_root_is_allowed(self, tmp_path):
        run = _install(tmp_path / "home", "eng")
        assert paths.resolve_root(run) == run.resolve()

    def test_bobi_root_env_pins_runtime(self, monkeypatch, tmp_path):
        run = _install(tmp_path / "home", "eng")
        monkeypatch.setenv("BOBI_ROOT", str(run))

        assert paths.resolve_root(tmp_path / "elsewhere") == run.resolve()

    def test_invalid_bobi_root_env_raises(self, monkeypatch, tmp_path):
        bogus = tmp_path / "bogus"
        bogus.mkdir()
        monkeypatch.setenv("BOBI_ROOT", str(bogus))

        with pytest.raises(RuntimeError, match="BOBI_ROOT"):
            paths.resolve_root()


class TestRuntimePaths:
    def test_runtime_paths_are_under_run_root(self, tmp_path):
        run = _install(tmp_path / "home", "eng")
        paths.bind_root(run)

        assert paths.package_dir() == run / "package"
        assert paths.agent_yaml_path() == run / "package" / "agent.yaml"
        assert paths.env_path() == run / ".env"
        assert paths.state_path() == run / "state"
        assert paths.workspace_dir() == run / "workspace"
        assert paths.sessions_dir() == run / "state" / "sessions"

    def test_agent_name_for_root(self, tmp_path):
        run = _install(tmp_path / "home", "eng")
        assert paths.agent_name_for_root(run) == "eng"

    def test_agent_name_derives_from_a_run_root(self, monkeypatch, tmp_path):
        """`<home>/agents/<name>/run` resolves to <name>, never the `run` leaf.

        This is the resolution the docker entrypoint performs, and the one a
        `requires:` probe's environment is built from (#1063): a hand-rolled
        `basename "$BOBI_ROOT"` reads `run`, an agent that does not exist,
        so every probe addressing the CLI fails on the canonical layout.
        """
        monkeypatch.delenv("BOBI_AGENT", raising=False)
        monkeypatch.delenv("BOBI_INSTANCE", raising=False)
        run = _install(tmp_path / "home", "eng")
        assert paths.agent_name(run) == "eng"

    def test_agent_name_prefers_an_explicit_override(self, monkeypatch,
                                                     tmp_path):
        run = _install(tmp_path / "home", "eng")
        monkeypatch.setenv("BOBI_AGENT", "override")
        assert paths.agent_name(run) == "override"
        # Same order as docker-entrypoint.sh: BOBI_AGENT, then BOBI_INSTANCE,
        # then the run root. An empty value is not a selection.
        monkeypatch.setenv("BOBI_AGENT", "")
        monkeypatch.setenv("BOBI_INSTANCE", "instance")
        assert paths.agent_name(run) == "instance"

    def test_agent_name_is_empty_when_unresolvable(self, monkeypatch):
        """No override and no name in the root: say so, don't invent one."""
        monkeypatch.delenv("BOBI_AGENT", raising=False)
        monkeypatch.delenv("BOBI_INSTANCE", raising=False)
        assert paths.agent_name(Path("/")) == ""

    def test_state_path_does_not_mkdir(self, tmp_path):
        run = tmp_path / "run"
        assert paths.state_path(run) == run / "state"
        assert not (run / "state").exists()


class TestUnboundRaises:
    def test_load_all_workflows_unbound_raises(self):
        from bobi.workflow.triggers import WorkflowDispatcher
        with pytest.raises(RuntimeError, match="not bound"):
            WorkflowDispatcher().load_all_workflows()
