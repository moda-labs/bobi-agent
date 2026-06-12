"""Contract tests for modastack.paths — the single root resolver and the
binding rules every process relies on."""

import os

import pytest

from modastack import paths


@pytest.fixture(autouse=True)
def unbound(monkeypatch):
    monkeypatch.setattr(paths, "_root", None)


def _install(root):
    (root / ".modastack").mkdir(parents=True)
    (root / ".modastack" / "agent.yaml").write_text("name: t\n")


class TestBindRoot:
    def test_binds_and_resolves_symlinks(self, tmp_path):
        real = tmp_path / "real"
        _install(real)
        link = tmp_path / "link"
        link.symlink_to(real)

        paths.bind_root(link)
        assert paths.modastack_root() == real

    def test_rebind_same_path_is_noop(self, tmp_path):
        paths.bind_root(tmp_path)
        paths.bind_root(tmp_path)
        assert paths.modastack_root() == tmp_path.resolve()

    def test_rebind_different_path_raises(self, tmp_path):
        """A process binds its identity exactly once — silently re-binding
        would split registry/state/log writes across two roots."""
        paths.bind_root(tmp_path / "a")
        with pytest.raises(RuntimeError, match="already bound"):
            paths.bind_root(tmp_path / "b")

    def test_none_unbinds(self, tmp_path):
        paths.bind_root(tmp_path)
        paths.bind_root(None)
        assert paths.bound_root() is None


class TestResolveRoot:
    def test_nearest_install_wins(self, tmp_path):
        """Nested installations resolve to the NEAREST marker — this is the
        capture behavior doctor's single-root check warns about, pinned
        here so a change to it is deliberate."""
        _install(tmp_path)
        nested = tmp_path / "sub"
        _install(nested)
        deeper = nested / "src" / "x"
        deeper.mkdir(parents=True)

        assert paths.resolve_root(deeper) == nested
        assert paths.resolve_root(tmp_path) == tmp_path

    def test_skips_linked_worktree(self, tmp_path):
        """A git linked worktree that carries .modastack/agent.yaml from
        its repo must NOT capture root resolution (#247). resolve_root
        detects linked worktrees via .git being a file (not a directory)
        and skips them, continuing to walk up to the real installation."""
        import subprocess as sp

        repo = tmp_path / "repo"
        _install(repo)
        sp.run(["git", "init", str(repo)], capture_output=True, check=True)
        sp.run(["git", "-C", str(repo), "config", "user.email", "t@t"], capture_output=True)
        sp.run(["git", "-C", str(repo), "config", "user.name", "t"], capture_output=True)
        sp.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        sp.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

        # Place worktree INSIDE the repo (matches real layout: .claude/worktrees/)
        wt = repo / ".claude" / "worktrees" / "feat"
        sp.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat", str(wt)],
               capture_output=True, check=True)

        # Worktree has the marker (checked-in) but .git is a file
        assert (wt / ".modastack" / "agent.yaml").is_file()
        assert (wt / ".git").is_file()

        deep = wt / "src"
        deep.mkdir()
        # Must skip the worktree and resolve to the main repo
        assert paths.resolve_root(deep) == repo

    def test_main_worktree_not_skipped(self, tmp_path):
        """The main working tree (.git is a directory) must still resolve
        normally — the worktree skip only applies to linked worktrees."""
        import subprocess as sp

        repo = tmp_path / "repo"
        _install(repo)
        sp.run(["git", "init", str(repo)], capture_output=True, check=True)

        assert (repo / ".git").is_dir()
        assert paths.resolve_root(repo) == repo

    def test_env_var_overrides_walk(self, tmp_path, monkeypatch):
        """MODASTACK_ROOT env var short-circuits the walk-up resolver,
        pinning the root for managed child processes (#247)."""
        real_root = tmp_path / "real"
        _install(real_root)

        decoy = tmp_path / "decoy"
        _install(decoy)

        monkeypatch.setenv("MODASTACK_ROOT", str(real_root))
        # Even starting from inside decoy, env var wins
        assert paths.resolve_root(decoy / "src") == real_root

    def test_env_var_invalid_falls_through(self, tmp_path, monkeypatch):
        """If MODASTACK_ROOT points to a non-installation, fall through
        to the normal walk-up resolver."""
        real_root = tmp_path / "real"
        _install(real_root)

        bogus = tmp_path / "bogus"
        bogus.mkdir()
        monkeypatch.setenv("MODASTACK_ROOT", str(bogus))

        deep = real_root / "src"
        deep.mkdir()
        assert paths.resolve_root(deep) == real_root


class TestIsLinkedWorktree:
    def test_linked_worktree_detected(self, tmp_path):
        """A linked worktree has a .git file starting with 'gitdir:'."""
        (tmp_path / ".git").write_text("gitdir: /some/path/.git/worktrees/foo\n")
        assert paths._is_linked_worktree(tmp_path) is True

    def test_main_repo_not_detected(self, tmp_path):
        """A main repo has a .git directory, not a file."""
        (tmp_path / ".git").mkdir()
        assert paths._is_linked_worktree(tmp_path) is False

    def test_no_git_not_detected(self, tmp_path):
        """A directory with no .git at all is not a worktree."""
        assert paths._is_linked_worktree(tmp_path) is False

    def test_git_file_without_gitdir_not_detected(self, tmp_path):
        """A .git file that doesn't start with 'gitdir:' is not a worktree."""
        (tmp_path / ".git").write_text("something else\n")
        assert paths._is_linked_worktree(tmp_path) is False


class TestNoSideEffects:
    def test_find_runtime_root_probe_creates_nothing(self, tmp_path):
        """The live-manager probe walks unowned ancestor dirs — it must
        never mkdir. Routing it through state_dir() (which creates) would
        recreate the scattered .modastack dirs this design removes."""
        from modastack.sdk import find_runtime_root
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)

        assert find_runtime_root(deep) is None
        assert not list(tmp_path.rglob(".modastack"))

    def test_state_path_does_not_mkdir(self, tmp_path):
        p = paths.state_path(tmp_path)
        assert not p.exists()


class TestUnboundRaises:
    def test_post_event_unbound_raises(self):
        from modastack.events.publish import post_event
        with pytest.raises(RuntimeError, match="not bound"):
            post_event("test.event", {})

    def test_load_all_workflows_unbound_raises(self):
        from modastack.workflow.triggers import WorkflowDispatcher
        with pytest.raises(RuntimeError, match="not bound"):
            WorkflowDispatcher().load_all_workflows()
