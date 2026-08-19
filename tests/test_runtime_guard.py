from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bobi import paths, runtime_guard
from bobi.doctor import _check_runtime_release_window, _check_runtime_write_policy
from bobi.runtime_guard import (
    apply_runtime_write_policy,
    check_bobi_distribution_integrity,
    check_runtime_write_policy,
    protected_runtime_roots,
    release_runtime_write_policy,
    release_window_status,
    reapply_runtime_write_policy,
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


def _framework_roots(tmp_path: Path) -> tuple[Path, Path, list]:
    site = tmp_path / "tool" / "lib" / "python3.13" / "site-packages"
    framework = site / "bobi"
    dist_info = site / "bobi-1.0.dist-info"
    framework.mkdir(parents=True)
    dist_info.mkdir()
    (framework / "module.py").write_text("x = 1\n")
    (dist_info / "METADATA").write_text("Name: bobi\n")
    roots = [
        runtime_guard.ProtectedRoot(framework, "bobi-package"),
        runtime_guard.ProtectedRoot(dist_info, "bobi-dist-info"),
    ]
    return framework, dist_info, roots


def _write_release_marker(home: Path, prefix: Path, expires_at: float, **overrides):
    payload = {
        "prefix": str(prefix),
        "expires_at": expires_at,
        "opened_by": "test",
        "pid": os.getpid(),
    }
    payload.update(overrides)
    home.mkdir(parents=True, exist_ok=True)
    (home / runtime_guard.RELEASE_MARKER).write_text(json.dumps(payload))


@pytest.fixture(autouse=True)
def _restore_tmp_write_bits(tmp_path):
    yield
    for path in sorted(tmp_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_symlink():
            try:
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
    try:
        tmp_path.chmod(tmp_path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


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

    def test_release_unlocks_framework_roots_but_not_team_package(
        self, tmp_path, monkeypatch,
    ):
        team = tmp_path / "team"
        framework, dist_info, framework_roots = _framework_roots(tmp_path)
        team.mkdir()
        (team / "file").write_text("x")
        for root in (team, framework, dist_info):
            for path in [*root.rglob("*"), root]:
                if path.is_symlink():
                    continue
                path.chmod(path.stat().st_mode & ~0o222)
        monkeypatch.setattr(
            runtime_guard, "framework_release_roots",
            lambda: (framework_roots, ""),
        )

        report = release_runtime_write_policy()

        assert [root.kind for root in report.released] == [
            "bobi-package", "bobi-dist-info"]
        assert not report.skipped
        assert not (team.stat().st_mode & stat.S_IWUSR)
        assert framework.stat().st_mode & stat.S_IWUSR
        assert dist_info.stat().st_mode & stat.S_IWUSR

    def test_release_allows_realistic_uv_tool_prefix_removal(
        self, tmp_path, monkeypatch,
    ):
        prefix = tmp_path / "uv" / "tools" / "bobi" / "lib" / "python3.12"
        package = prefix / "site-packages" / "bobi"
        dist_info = prefix / "site-packages" / "bobi.dist-info"
        (package / "nested").mkdir(parents=True)
        dist_info.mkdir()
        (package / "nested" / "module.py").write_text("x = 1\n")
        (dist_info / "METADATA").write_text("Name: bobi\n")
        roots = [
            runtime_guard.ProtectedRoot(package, "bobi-package"),
            runtime_guard.ProtectedRoot(dist_info, "bobi-dist-info"),
        ]
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)
        monkeypatch.setattr(runtime_guard, "framework_release_roots", lambda: (roots, ""))
        guard_report = apply_runtime_write_policy(None)

        assert not guard_report.skipped
        with pytest.raises(PermissionError):
            shutil.rmtree(prefix.parent.parent)

        report = release_runtime_write_policy()
        shutil.rmtree(prefix.parent.parent)

        assert not report.skipped
        assert not (tmp_path / "uv" / "tools" / "bobi").exists()

    def test_release_reports_partial_failure(self, tmp_path, monkeypatch):
        framework, _, roots = _framework_roots(tmp_path)
        denied = framework / "denied"
        denied.write_text("x")
        for path in [*framework.rglob("*"), framework]:
            path.chmod(path.stat().st_mode & ~0o222)
        monkeypatch.setattr(
            runtime_guard, "framework_release_roots", lambda: (roots, ""),
        )
        real_chmod = os.chmod

        def chmod(path, mode, **kwargs):
            if Path(path) == denied and mode & stat.S_IWUSR:
                raise PermissionError(1, "Operation not permitted", str(path))
            return real_chmod(path, mode, **kwargs)

        monkeypatch.setattr(os, "chmod", chmod)

        report = release_runtime_write_policy()

        assert report.skipped
        assert any(str(denied) in failure for failure in report.skipped)
        assert not runtime_guard.release_marker_path().exists()
        assert not framework.stat().st_mode & stat.S_IWUSR

    def test_release_is_honest_noop(self, monkeypatch):
        monkeypatch.setattr(
            runtime_guard, "framework_release_roots",
            lambda: ([], "editable install"),
        )

        report = release_runtime_write_policy()

        assert not report.roots
        assert not report.skipped
        assert report.noop_reason == "editable install"

    @pytest.mark.parametrize("payload", [
        "not-json",
        json.dumps({"prefix": "/tmp", "expires_at": time.time() + 3600}),
    ])
    def test_malformed_marker_fails_closed(self, tmp_path, monkeypatch, payload):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        home.mkdir()
        runtime_guard.release_marker_path().write_text(payload)
        framework, _, roots = _framework_roots(tmp_path)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        report = apply_runtime_write_policy(None)

        assert not report.released
        assert not framework.stat().st_mode & stat.S_IWUSR
        assert release_window_status().state == "invalid"

    def test_future_dated_marker_fails_closed(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        _write_release_marker(home, tmp_path, time.time() + 86400)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        report = apply_runtime_write_policy(None)

        assert not report.released
        assert release_window_status().state == "invalid"
        assert not framework.stat().st_mode & stat.S_IWUSR

    def test_expired_window_resumes_writable_drift_detection(
        self, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        _write_release_marker(home, tmp_path / "tool", time.time() - 1)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        result = check_runtime_write_policy(None)

        assert not result.ok
        assert result.window.state == "expired"
        assert any(str(framework) in failure for failure in result.failures)

    def test_prefix_mismatch_does_not_cover_framework(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        _write_release_marker(home, tmp_path / "other", time.time() + 60)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        report = apply_runtime_write_policy(None)

        assert not report.released
        assert not framework.stat().st_mode & stat.S_IWUSR

    def test_open_window_skips_framework_but_locks_team(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        team = tmp_path / "team"
        team.mkdir()
        (team / "agent.yaml").write_text("agent: test\n")
        team_root = runtime_guard.ProtectedRoot(team, "team-package")
        _write_release_marker(home, tmp_path / "tool", time.time() + 60)
        monkeypatch.setattr(
            runtime_guard, "protected_runtime_roots", lambda _: [team_root, *roots],
        )

        report = apply_runtime_write_policy(tmp_path)

        assert [root.kind for root in report.released] == [
            "bobi-package", "bobi-dist-info"]
        assert framework.stat().st_mode & stat.S_IWUSR
        assert not team.stat().st_mode & stat.S_IWUSR

    def test_release_retries_after_stale_apply_race(self, tmp_path, monkeypatch):
        framework, _, roots = _framework_roots(tmp_path)
        for root in roots:
            runtime_guard._chmod_tree(root.path, runtime_guard._readonly_mode)
        monkeypatch.setattr(runtime_guard, "framework_release_roots", lambda: (roots, ""))
        real_chmod_tree = runtime_guard._chmod_tree
        raced = False

        def racing_chmod(root, mode_fn, **kwargs):
            nonlocal raced
            result = real_chmod_tree(root, mode_fn, **kwargs)
            if mode_fn is runtime_guard._mutable_mode and not raced:
                raced = True
                real_chmod_tree(root, runtime_guard._readonly_mode)
            return result

        monkeypatch.setattr(runtime_guard, "_chmod_tree", racing_chmod)

        report = release_runtime_write_policy()

        assert report.ok
        assert raced
        assert framework.stat().st_mode & stat.S_IWUSR

    def test_release_does_not_chmod_when_marker_cannot_be_written(
        self, tmp_path, monkeypatch,
    ):
        framework, _, roots = _framework_roots(tmp_path)
        runtime_guard._chmod_tree(framework, runtime_guard._readonly_mode)
        monkeypatch.setattr(runtime_guard, "framework_release_roots", lambda: (roots, ""))
        monkeypatch.setattr(
            runtime_guard.fsutil,
            "atomic_write_json",
            lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
        )

        report = release_runtime_write_policy()

        assert not report.ok
        assert "could not open release window" in report.skipped[0]
        assert not framework.stat().st_mode & stat.S_IWUSR

    def test_reapply_closes_window_and_locks_framework(self, tmp_path, monkeypatch):
        framework, _, roots = _framework_roots(tmp_path)
        monkeypatch.setattr(runtime_guard, "framework_release_roots", lambda: (roots, ""))
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)
        release = release_runtime_write_policy()
        assert release.ok

        report = reapply_runtime_write_policy()

        assert not report.marker_error
        assert not runtime_guard.release_marker_path().exists()
        assert not framework.stat().st_mode & stat.S_IWUSR

    def test_window_suppresses_writable_drift_but_not_symlink_escape(
        self, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        outside = tmp_path / "outside"
        outside.write_text("x")
        (framework / "escape").symlink_to(outside)
        _write_release_marker(home, tmp_path / "tool", time.time() + 60)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        result = check_runtime_write_policy(None)

        assert not result.ok
        assert any("symlink escapes" in failure for failure in result.failures)

    def test_team_mutation_window_relocks_while_framework_window_is_open(
        self, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        team = _write_runtime(tmp_path / "runtime")
        runtime_guard._chmod_tree(team, runtime_guard._readonly_mode)
        _write_release_marker(home, tmp_path / "tool", time.time() + 60)

        with with_mutable_runtime_package(tmp_path / "runtime"):
            assert team.stat().st_mode & stat.S_IWUSR

        assert not team.stat().st_mode & stat.S_IWUSR

    def test_doctor_names_release_step_when_framework_is_locked(
        self, tmp_path, monkeypatch,
    ):
        framework = runtime_guard.ProtectedRoot(
            tmp_path / "bobi", "bobi-package")
        monkeypatch.setattr("bobi.doctor.bound_root", lambda: tmp_path)
        monkeypatch.setattr(
            runtime_guard,
            "check_runtime_write_policy",
            lambda _: runtime_guard.PolicyCheck(
                ok=True, detail="1 protected runtime root(s)",
                protected=[framework],
            ),
        )

        result = _check_runtime_write_policy()

        assert result.ok
        assert "bobi guard release" in result.detail

    def test_doctor_reports_open_window_as_warning(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        framework, _, roots = _framework_roots(tmp_path)
        _write_release_marker(home, tmp_path / "tool", time.time() + 60)
        monkeypatch.setattr("bobi.doctor.bound_root", lambda: tmp_path)
        monkeypatch.setattr(runtime_guard, "protected_runtime_roots", lambda _: roots)

        result = _check_runtime_release_window()

        assert result is not None
        assert not result.ok
        assert not result.required
        assert "open by test" in result.detail
        assert str(runtime_guard.release_marker_path()) in result.detail

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
         patch("bobi.brain.turns.save_session_id"), \
         patch("bobi.brain.turns.log_activity"), \
         patch("bobi.subagent.get_registry", return_value=MagicMock()):
        result = await _run_agent_supervised(
            prompt="check", cwd="/tmp", run_key="k", phase="check", timeout=5,
        )

    assert result.success is True
    assert events[:2] == ["prepare", "make_session"]
