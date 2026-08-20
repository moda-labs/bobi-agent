from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bobi import paths
from bobi.runtime_guard import (
    apply_runtime_write_policy,
    check_bobi_distribution_integrity,
    check_runtime_write_policy,
    protected_runtime_roots,
    with_mutable_runtime_package,
)


def _write_runtime(root: Path) -> Path:
    package = paths.package_dir(root)
    package.mkdir(parents=True)
    paths.agent_yaml_path(root).write_text("agent: test\n")
    (package / "roles").mkdir()
    (package / "roles" / "ROLE.md").write_text("# Role\n")
    paths.workspace_dir(root).mkdir()
    paths.state_dir(root)
    return package


def _sha256_record_value(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


class TestRuntimeWritePolicy:
    def test_protected_roots_strictly_contains_team_package_only(self, tmp_path):
        package = _write_runtime(tmp_path)

        roots = protected_runtime_roots(tmp_path)

        assert len(roots) == 1
        assert roots[0].path == package
        assert roots[0].kind == "team-package"
        assert not any(r.kind in ("bobi-package", "bobi-dist-info") for r in roots)

    def test_check_fails_for_writable_package_file(self, tmp_path):
        _write_runtime(tmp_path)

        result = check_runtime_write_policy(tmp_path)

        assert not result.ok
        assert "writable" in result.detail
        assert "agent.yaml" in result.detail or result.failures

    def test_apply_policy_tolerates_unowned_files(self, tmp_path, monkeypatch):
        package = _write_runtime(tmp_path)
        denied = package / "roles" / "ROLE.md"
        real_chmod = os.chmod

        def chmod(path, mode, **kwargs):
            if Path(path) == denied:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_chmod(path, mode, **kwargs)

        monkeypatch.setattr(os, "chmod", chmod)

        report = apply_runtime_write_policy(tmp_path)

        assert any(root.kind == "team-package" for root in report.protected)
        assert any("ROLE.md" in entry for entry in report.skipped)
        agent_yaml = paths.agent_yaml_path(tmp_path)
        assert not (agent_yaml.stat().st_mode & 0o222)

    def test_mutable_window_fails_loud_on_unowned_files(self, tmp_path, monkeypatch):
        package = _write_runtime(tmp_path)
        denied = package / "roles" / "ROLE.md"
        real_chmod = os.chmod

        def chmod(path, mode, **kwargs):
            if Path(path) == denied:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_chmod(path, mode, **kwargs)

        monkeypatch.setattr(os, "chmod", chmod)

        entered = False
        with pytest.raises(PermissionError):
            with with_mutable_runtime_package(tmp_path):
                entered = True

        assert not entered

    def test_a_failed_unlock_relocks_what_it_already_opened(self, tmp_path,
                                                            monkeypatch):
        """D044 — the +w sweep ran BEFORE the try, so its finally never fired.

        A package image containing one file owned by another uid (the exact
        class #774 handled for the readonly direction) raises partway through
        the unlock. Every file chmodded before that point stayed writable, with
        no rollback — the protected tree sits half-unlocked, failing doctor's
        write-policy check, until the next subagent spawn happens to re-run
        prepare_brain_runtime.
        """
        package = _write_runtime(tmp_path)
        # Everything starts locked, as a real installed image does.
        for path in [*package.rglob("*"), package]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)

        real_chmod = os.chmod
        unlocked: list[Path] = []

        def chmod(path, mode, **kwargs):
            if mode & stat.S_IWUSR:          # the mutable (+w) sweep
                if len(unlocked) >= 1:       # ...fails partway through it
                    raise PermissionError(1, "Operation not permitted", str(path))
                unlocked.append(Path(path))
            return real_chmod(path, mode, **kwargs)

        monkeypatch.setattr(os, "chmod", chmod)

        with pytest.raises(PermissionError):
            with with_mutable_runtime_package(tmp_path):
                pass

        monkeypatch.undo()
        assert unlocked, "the test never opened anything — nothing to roll back"
        still_writable = [
            str(p) for p in package.rglob("*")
            if not p.is_symlink() and p.stat().st_mode & 0o222
        ]
        assert not still_writable, (
            f"a failed unlock left the tree writable: {still_writable}")

    def test_check_fails_for_symlink_escaping_package(self, tmp_path):
        package = _write_runtime(tmp_path)
        target = tmp_path / "outside.txt"
        target.write_text("outside\n")
        (package / "escape").symlink_to(target)
        for path in [*package.rglob("*"), package]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)

        result = check_runtime_write_policy(tmp_path)

        assert not result.ok
        assert any("symlink escapes" in failure for failure in result.failures)


class _FakeFile:
    def __init__(self, value: str, hash_value: str | None = None):
        self.value = value
        self.hash = (
            SimpleNamespace(mode="sha256", value=hash_value)
            if hash_value is not None else None
        )

    def __str__(self):
        return self.value


class _FakeDist:
    def __init__(self, root: Path, files, entry_points=(), direct_url_text: str | None = None):
        self.root = root
        self.files = files
        self.entry_points = entry_points
        self._direct_url_text = direct_url_text

    def locate_file(self, file):
        return self.root / Path(str(file))

    def read_text(self, filename: str):
        if filename == "direct_url.json":
            return self._direct_url_text
        return None


class TestBobiDistributionIntegrity:
    def test_direct_url_editable_passes(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        package.mkdir(parents=True)
        monkeypatch.setattr("bobi.__file__", str(package / "__init__.py"))
        dist = _FakeDist(tmp_path, files=[], direct_url_text='{"editable": true}')

        result = check_bobi_distribution_integrity(dist)

        assert result.ok
        assert "editable" in result.detail

    def test_direct_source_checkout_passes(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[project]\nname = 'bobi'\n")
        (repo_root / ".git").mkdir()
        package = repo_root / "bobi"
        package.mkdir()
        init = package / "__init__.py"
        init.write_text("# source\n")
        monkeypatch.setattr("bobi.__file__", str(init))

        result = check_bobi_distribution_integrity(None)

        assert result.ok
        assert "source checkout" in result.detail

    def test_missing_dist_on_non_source_checkout_fails(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        package.mkdir(parents=True)
        init = package / "__init__.py"
        init.write_text("# installed\n")
        monkeypatch.setattr("bobi.__file__", str(init))

        result = check_bobi_distribution_integrity(None)

        assert not result.ok
        assert "distribution metadata not found" in result.detail

    def test_missing_record_metadata_on_non_source_fails(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        package.mkdir(parents=True)
        init = package / "__init__.py"
        init.write_text("# installed\n")
        monkeypatch.setattr("bobi.__file__", str(init))
        dist = _FakeDist(tmp_path / "site-packages", files=None)

        result = check_bobi_distribution_integrity(dist)

        assert not result.ok
        assert "no RECORD metadata" in result.detail

    def test_zero_hashed_files_fails(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("content\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [_FakeFile("bobi-1.0.dist-info/RECORD")],
        )

        result = check_bobi_distribution_integrity(dist)

        assert not result.ok
        assert "0 hashed Bobi file(s) verified" in result.detail

    def test_hashed_bobi_file_mismatch_fails(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("original\n")
        good = _sha256_record_value(b"original\n")
        source.write_text("edited\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [
                _FakeFile("bobi/__init__.py", good),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )

        result = check_bobi_distribution_integrity(dist)

        assert not result.ok
        assert "sha256 mismatch" in result.detail

    def test_non_sha256_hashes_are_skipped(self, tmp_path, monkeypatch):
        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("content\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        file = _FakeFile("bobi/__init__.py")
        file.hash = SimpleNamespace(mode="md5", value="bad")
        dist = _FakeDist(tmp_path / "site-packages", [file])

        result = check_bobi_distribution_integrity(dist)

        assert not result.ok
        assert "0 hashed" in result.detail

    def test_hashed_file_outside_distribution_roots_fails(self, tmp_path, monkeypatch):
        site = tmp_path / "site-packages"
        package = site / "bobi"
        dist_info = site / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("content\n")
        outside = site / "other.py"
        outside.write_text("outside\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            site,
            [
                _FakeFile("other.py", _sha256_record_value(b"outside\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )

        result = check_bobi_distribution_integrity(dist)

        assert not result.ok
        assert "outside Bobi distribution roots" in result.detail

    def test_generated_console_script_record_entry_is_skipped(
        self, tmp_path, monkeypatch,
    ):
        site = tmp_path / "venv" / "lib" / "python3.13" / "site-packages"
        package = site / "bobi"
        dist_info = site / "bobi-1.0.dist-info"
        bin_dir = tmp_path / "venv" / "bin"
        package.mkdir(parents=True)
        dist_info.mkdir()
        bin_dir.mkdir(parents=True)
        source = package / "__init__.py"
        source.write_text("content\n")
        script = bin_dir / "bobi"
        script.write_text("#!/bin/sh\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            site,
            [
                _FakeFile("../../../bin/bobi", _sha256_record_value(b"#!/bin/sh\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
                _FakeFile("bobi/__init__.py", _sha256_record_value(b"content\n")),
            ],
            entry_points=[
                SimpleNamespace(group="console_scripts", name="bobi"),
            ],
        )

        result = check_bobi_distribution_integrity(dist)

        assert result.ok


class TestFrameworkIntegrityVerification:
    def test_verify_framework_integrity_or_raise_passes_for_clean_dist(self, tmp_path, monkeypatch):
        from bobi.runtime_guard import verify_framework_integrity_or_raise

        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("clean\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [
                _FakeFile("bobi/__init__.py", _sha256_record_value(b"clean\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )

        check = verify_framework_integrity_or_raise(dist=dist)
        assert check.ok

    def test_verify_framework_integrity_or_raise_fails_on_tampered_file(self, tmp_path, monkeypatch):
        from bobi.runtime_guard import verify_framework_integrity_or_raise

        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("tampered\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [
                _FakeFile("bobi/__init__.py", _sha256_record_value(b"original\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )

        with pytest.raises(RuntimeError, match="Bobi framework integrity violation detected"):
            verify_framework_integrity_or_raise(dist=dist)

    def test_prepare_brain_runtime_passes_on_clean_distribution_via_default_distribution(
        self, tmp_path, monkeypatch
    ):
        from bobi.runtime_guard import prepare_brain_runtime

        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("clean\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [
                _FakeFile("bobi/__init__.py", _sha256_record_value(b"clean\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )
        monkeypatch.setattr("bobi.runtime_guard._distribution", lambda name: dist)

        report = prepare_brain_runtime()
        assert report is not None

    def test_prepare_brain_runtime_fails_closed_when_framework_is_compromised(self, tmp_path, monkeypatch):
        from bobi.runtime_guard import prepare_brain_runtime

        package = tmp_path / "site-packages" / "bobi"
        dist_info = tmp_path / "site-packages" / "bobi-1.0.dist-info"
        package.mkdir(parents=True)
        dist_info.mkdir()
        source = package / "__init__.py"
        source.write_text("compromised\n")
        monkeypatch.setattr("bobi.__file__", str(source))
        dist = _FakeDist(
            tmp_path / "site-packages",
            [
                _FakeFile("bobi/__init__.py", _sha256_record_value(b"expected\n")),
                _FakeFile("bobi-1.0.dist-info/RECORD"),
            ],
        )
        monkeypatch.setattr("bobi.runtime_guard._distribution", lambda name: dist)

        with pytest.raises(RuntimeError, match="Bobi framework integrity violation"):
            prepare_brain_runtime()


def test_session_prepares_runtime_before_brain_session():
    from bobi.session import Session

    brain = MagicMock()
    brain.make_session.return_value = object()
    session = Session(name="s", cwd="/tmp")
    session._brain = brain

    with patch("bobi.runtime_guard.prepare_brain_runtime") as prepare:
        session._make_brain_session()

    prepare.assert_called_once()
    brain.make_session.assert_called_once()


@pytest.mark.asyncio
async def test_supervised_agent_prepares_runtime_before_provider_client(monkeypatch):
    from tests.test_subagent_blocking import _CapturingBrainSession
    from bobi.subagent import _run_agent_supervised

    events: list[str] = []

    class FakeBrain:
        def make_session(self, **kwargs):
            events.append("make_session")
            return _CapturingBrainSession()

    def prepare():
        events.append("prepare")

    with patch("bobi.brain.get_brain", lambda kind=None: FakeBrain()), \
         patch("bobi.runtime_guard.prepare_brain_runtime", side_effect=prepare), \
         patch("bobi.subagent.load_resumable_session_id", return_value=""), \
         patch("bobi.subagent.save_session_id"), \
         patch("bobi.brain.turns.save_session_id"), \
         patch("bobi.brain.turns.log_activity"), \
         patch("bobi.subagent.get_registry", return_value=MagicMock()):
        result = await _run_agent_supervised(
            prompt="check", cwd="/tmp", run_key="k", phase="check", timeout=5,
        )

    assert result.success is True
    assert events[:2] == ["prepare", "make_session"]


@pytest.mark.asyncio
async def test_setup_llm_stream_prepares_runtime_before_stream_once():
    from bobi.setup.llm import _sdk_stream

    events: list[str] = []

    class FakeBrain:
        async def stream_once(self, **kwargs):
            events.append("stream_once")
            yield MagicMock(text="response")

    def prepare():
        events.append("prepare")

    with patch("bobi.brain.get_brain", return_value=FakeBrain()), \
         patch("bobi.runtime_guard.prepare_brain_runtime", side_effect=prepare):
        async for _ in _sdk_stream(system_prompt="s", user_prompt="u"):
            pass

    assert events == ["prepare", "stream_once"]


@pytest.mark.asyncio
async def test_validate_mcp_probe_prepares_runtime_before_probe_session(tmp_path):
    from bobi.validate import _async_probe_mcp

    events: list[str] = []
    brain = MagicMock()
    brain.make_session.side_effect = lambda **kwargs: events.append("make_session")

    def prepare(path=None):
        events.append("prepare")

    with patch("bobi.brain.get_brain", return_value=brain), \
         patch("bobi.runtime_guard.prepare_brain_runtime", side_effect=prepare), \
         patch("bobi.validate.child_agent_env", return_value={}):
        try:
            await _async_probe_mcp(["server1"], {"server1": {}}, tmp_path)
        except Exception:
            pass

    assert "prepare" in events


def test_workflow_orchestrator_make_session_prepares_runtime(tmp_path, monkeypatch):
    from bobi.workflow.schema import Workflow, StepDef
    from bobi.workflow.orchestrator import run_workflow

    events: list[str] = []

    class FakeBrainSession:
        async def submit_turn(self, *args, **kwargs):
            return MagicMock(stop_reason="end_turn", is_error=False, text="ok")

    class FakeBrain:
        def make_session(self, **kwargs):
            events.append("make_session")
            return FakeBrainSession()

    def prepare():
        events.append("prepare")

    wf = Workflow(name="w", steps=[StepDef(name="s1", prompt="p", model="haiku")])

    paths.package_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    paths.agent_yaml_path(tmp_path).write_text("agent: test\nentry_point: manager\n")
    monkeypatch.setattr(paths, "_root", None)
    paths.bind_root(tmp_path)

    registry = MagicMock()
    cwd = str(tmp_path)

    with patch("bobi.brain.get_brain", return_value=FakeBrain()), \
         patch("bobi.runtime_guard.prepare_brain_runtime", side_effect=prepare), \
         patch("bobi.workflow.orchestrator.get_registry", return_value=registry), \
         patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
         patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
         patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
         patch("bobi.workflow.orchestrator.save_session_id"), \
         patch("bobi.brain.turns.save_session_id"), \
         patch("bobi.workflow.orchestrator.log_activity"), \
         patch("bobi.brain.turns.log_activity"):
        try:
            run_workflow(wf, task="t", repo="r", cwd=cwd, run_key="1")
        except Exception:
            pass

    assert "prepare" in events
