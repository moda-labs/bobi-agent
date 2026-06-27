"""Tests for bobi.state_version — format version marker."""

import pytest

from bobi import paths
from bobi.state_version import (
    CURRENT_FORMAT_VERSION,
    StateVersionError,
    ensure_state_version,
    format_version_path,
)


@pytest.fixture(autouse=True)
def _unbound(monkeypatch):
    monkeypatch.setattr(paths, "_root", None)
    monkeypatch.delenv("BOBI_ROOT", raising=False)


def _install(root):
    paths.package_dir(root).mkdir(parents=True)
    paths.agent_yaml_path(root).write_text("name: t\n")


class TestEnsureStateVersion:
    def test_writes_version_on_first_run(self, tmp_path):
        """First run (no format_version file) writes the current version."""
        _install(tmp_path)
        ensure_state_version(tmp_path)

        fv = format_version_path(tmp_path)
        assert fv.exists()
        assert int(fv.read_text().strip()) == CURRENT_FORMAT_VERSION

    def test_noop_when_version_matches(self, tmp_path):
        """When on-disk version matches current, ensure is a no-op."""
        _install(tmp_path)
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        fv = state / "format_version"
        fv.write_text(f"{CURRENT_FORMAT_VERSION}\n")

        ensure_state_version(tmp_path)
        assert int(fv.read_text().strip()) == CURRENT_FORMAT_VERSION

    def test_refuses_newer_version(self, tmp_path):
        """On-disk version newer than code → StateVersionError."""
        _install(tmp_path)
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        fv = state / "format_version"
        fv.write_text(f"{CURRENT_FORMAT_VERSION + 1}\n")

        with pytest.raises(StateVersionError, match="newer bobi"):
            ensure_state_version(tmp_path)

    def test_upgrades_older_version(self, tmp_path):
        """On-disk version older than code → stamps current version
        (future: runs migrations first)."""
        _install(tmp_path)
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        fv = state / "format_version"
        fv.write_text("0\n")

        ensure_state_version(tmp_path)
        assert int(fv.read_text().strip()) == CURRENT_FORMAT_VERSION

    def test_corrupt_version_raises(self, tmp_path):
        """Non-integer content in format_version → StateVersionError."""
        _install(tmp_path)
        state = paths.state_path(tmp_path)
        state.mkdir(parents=True)
        fv = state / "format_version"
        fv.write_text("not-a-number\n")

        with pytest.raises(StateVersionError, match="Corrupt"):
            ensure_state_version(tmp_path)

    def test_creates_state_dir_if_missing(self, tmp_path):
        """ensure_state_version creates the state directory if needed."""
        _install(tmp_path)
        state = paths.state_path(tmp_path)
        assert not state.exists()

        ensure_state_version(tmp_path)

        assert state.exists()
        assert format_version_path(tmp_path).exists()


class TestFormatVersionPath:
    def test_path_under_state(self, tmp_path):
        p = format_version_path(tmp_path)
        assert p == tmp_path / "state" / "format_version"
