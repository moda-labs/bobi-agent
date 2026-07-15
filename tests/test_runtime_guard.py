from __future__ import annotations

import base64
import hashlib
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
    def test_protected_roots_include_bound_team_package(self, tmp_path):
        package = _write_runtime(tmp_path)

        roots = protected_runtime_roots(tmp_path)

        assert any(root.path == package and root.kind == "team-package" for root in roots)

    def test_check_fails_for_writable_package_file(self, tmp_path):
        _write_runtime(tmp_path)

        result = check_runtime_write_policy(tmp_path)

        assert not result.ok
        assert "writable" in result.detail
        assert "agent.yaml" in result.detail or result.failures

    def test_apply_policy_tolerates_unowned_files(self, tmp_path, monkeypatch):
        import os

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
        agent_yaml = paths.agent_yaml_path(tmp_path)
        assert not (agent_yaml.stat().st_mode & 0o222)

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
    def __init__(self, root: Path, files, entry_points=()):
        self.root = root
        self.files = files
        self.entry_points = entry_points

    def locate_file(self, file):
        return self.root / Path(str(file))


class TestBobiDistributionIntegrity:
    def test_editable_source_without_record_passes(self):
        dist = SimpleNamespace(files=None)

        result = check_bobi_distribution_integrity(dist)

        assert result.ok
        assert "editable" in result.detail

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

        assert result.ok
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
            ],
            entry_points=[
                SimpleNamespace(group="console_scripts", name="bobi"),
            ],
        )

        result = check_bobi_distribution_integrity(dist)

        assert result.ok


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
         patch("bobi.subagent.log_activity"), \
         patch("bobi.subagent.get_registry", return_value=MagicMock()):
        result = await _run_agent_supervised(
            prompt="check", cwd="/tmp", run_key="k", phase="check", timeout=5,
        )

    assert result.success is True
    assert events[:2] == ["prepare", "make_session"]
