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
