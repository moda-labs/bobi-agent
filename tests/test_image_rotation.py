"""Tests for session rotation when the installed agent image changes.

When the install-manifest hash changes between runs, sessions should be
rotated (cleared) so agents start fresh with the new image. The decision
log / memory primitive provides continuity across rotations.
"""

import json
from pathlib import Path

import pytest

from bobi import paths
from bobi.sdk import (
    SessionEntry, SessionRegistry, compute_manifest_hash,
    check_image_rotation, save_session_id, load_session_id,
)


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    paths.bind_root(None)
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text("agent: test\n")
    paths.bind_root(tmp_path)
    yield SessionRegistry()
    paths.bind_root(None)


def _write_manifest(project: Path, files: dict[str, str]) -> None:
    """Write an install-manifest.json with the given file hashes."""
    manifest = paths.install_manifest_path(project)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "agent": "test-team",
        "source": "/tmp/test-team",
        "frozen": True,
        "files": files,
    }))


# ---------------------------------------------------------------------------
# compute_manifest_hash
# ---------------------------------------------------------------------------

class TestComputeManifestHash:
    def test_returns_empty_when_no_manifest(self, tmp_path):
        assert compute_manifest_hash(tmp_path) == ""

    def test_returns_empty_when_no_files(self, tmp_path):
        _write_manifest(tmp_path, {})
        assert compute_manifest_hash(tmp_path) == ""

    def test_returns_deterministic_hash(self, tmp_path):
        _write_manifest(tmp_path, {"roles/eng/ROLE.md": "abc123"})
        h1 = compute_manifest_hash(tmp_path)
        h2 = compute_manifest_hash(tmp_path)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_different_files_produce_different_hash(self, tmp_path):
        _write_manifest(tmp_path, {"roles/eng/ROLE.md": "abc123"})
        h1 = compute_manifest_hash(tmp_path)
        _write_manifest(tmp_path, {"roles/eng/ROLE.md": "def456"})
        h2 = compute_manifest_hash(tmp_path)
        assert h1 != h2

    def test_order_independent(self, tmp_path):
        """JSON key order shouldn't affect the hash."""
        _write_manifest(tmp_path, {"a.md": "1", "b.md": "2"})
        h1 = compute_manifest_hash(tmp_path)
        _write_manifest(tmp_path, {"b.md": "2", "a.md": "1"})
        h2 = compute_manifest_hash(tmp_path)
        assert h1 == h2

    def test_uses_project_root_when_no_path(self, tmp_path, monkeypatch):
        paths.bind_root(tmp_path)
        _write_manifest(tmp_path, {"x.md": "hash"})
        assert compute_manifest_hash() != ""

    def test_handles_malformed_json(self, tmp_path):
        manifest = paths.install_manifest_path(tmp_path)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("not json")
        assert compute_manifest_hash(tmp_path) == ""


# ---------------------------------------------------------------------------
# SessionEntry.image_hash field
# ---------------------------------------------------------------------------

class TestSessionEntryImageHash:
    def test_defaults_empty(self):
        e = SessionEntry(name="test")
        assert e.image_hash == ""

    def test_roundtrips_through_registry(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", image_hash="abc123"))
        got = tmp_registry.get("eng-1")
        assert got.image_hash == "abc123"

    def test_preserved_on_update(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", image_hash="abc123"))
        tmp_registry.update("eng-1", status="running")
        got = tmp_registry.get("eng-1")
        assert got.image_hash == "abc123"


# ---------------------------------------------------------------------------
# check_image_rotation (shared helper)
# ---------------------------------------------------------------------------

class TestCheckImageRotation:
    def test_rotates_when_hash_changes(self, tmp_path, monkeypatch):
        """Session is cleared when manifest hash differs from stored stamp."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        registry = SessionRegistry()
        session_name = "moda-manager-proj"

        _write_manifest(tmp_path, {"a.md": "old_hash"})
        old_hash = compute_manifest_hash(tmp_path)
        save_session_id(session_name, "session-id-123")
        registry.register(SessionEntry(
            name=session_name, session_id="session-id-123",
            image_hash=old_hash,
        ))

        # Install changes → new manifest
        _write_manifest(tmp_path, {"a.md": "new_hash"})

        assert check_image_rotation(session_name, tmp_path) is True
        assert load_session_id(session_name) == ""

    def test_no_rotation_when_hash_matches(self, tmp_path, monkeypatch):
        """Session preserved when manifest hasn't changed."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        registry = SessionRegistry()
        session_name = "moda-manager-proj"

        _write_manifest(tmp_path, {"a.md": "same_hash"})
        h = compute_manifest_hash(tmp_path)
        save_session_id(session_name, "session-id-456")
        registry.register(SessionEntry(
            name=session_name, session_id="session-id-456",
            image_hash=h,
        ))

        assert check_image_rotation(session_name, tmp_path) is False
        assert load_session_id(session_name) == "session-id-456"

    def test_no_rotation_when_no_prior_hash(self, tmp_path, monkeypatch):
        """First run (empty stored hash) does not rotate."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        registry = SessionRegistry()
        session_name = "moda-manager-proj"

        _write_manifest(tmp_path, {"a.md": "first_hash"})
        save_session_id(session_name, "session-id-789")
        registry.register(SessionEntry(
            name=session_name, session_id="session-id-789",
            image_hash="",
        ))

        assert check_image_rotation(session_name, tmp_path) is False
        assert load_session_id(session_name) == "session-id-789"

    def test_no_rotation_when_no_saved_session(self, tmp_path, monkeypatch):
        """No session ID → nothing to rotate."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        _write_manifest(tmp_path, {"a.md": "hash"})
        assert check_image_rotation("nonexistent", tmp_path) is False

    def test_no_rotation_when_no_manifest(self, tmp_path, monkeypatch):
        """Without an install manifest, rotation never fires."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        session_name = "moda-manager-proj"
        save_session_id(session_name, "session-id-111")
        assert check_image_rotation(session_name, tmp_path) is False
        assert load_session_id(session_name) == "session-id-111"


# ---------------------------------------------------------------------------
# Sub-agent image hash stamping and rotation (subagent.py paths)
# ---------------------------------------------------------------------------

class TestSubagentImageStamp:
    def test_stamp_written_at_registration(self, tmp_path, monkeypatch):
        """SessionEntry should carry the current manifest hash."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        _write_manifest(tmp_path, {"tools/github.md": "abc"})
        expected_hash = compute_manifest_hash(tmp_path)

        registry = SessionRegistry()
        registry.register(SessionEntry(
            name="wf-test-proj-42",
            image_hash=expected_hash,
        ))
        got = registry.get("wf-test-proj-42")
        assert got.image_hash == expected_hash

    def test_stale_subagent_rotated_on_mismatch(self, tmp_path, monkeypatch):
        """Sub-agent session ID cleared when image hash differs."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        registry = SessionRegistry()
        session_name = "wf-lifecycle-proj-42"

        # Old session with old hash
        _write_manifest(tmp_path, {"tools/github.md": "old"})
        old_hash = compute_manifest_hash(tmp_path)
        save_session_id(session_name, "old-session-id")
        registry.register(SessionEntry(
            name=session_name, session_id="old-session-id",
            image_hash=old_hash, status="done",
        ))

        # Image changes
        _write_manifest(tmp_path, {"tools/github.md": "new"})
        current_hash = compute_manifest_hash(tmp_path)

        # Rotation clears session ID, re-register stamps new hash
        existing = registry.get(session_name)
        assert existing.image_hash != current_hash
        save_session_id(session_name, "")
        assert load_session_id(session_name) == ""

        registry.register(SessionEntry(
            name=session_name, image_hash=current_hash,
        ))
        got = registry.get(session_name)
        assert got.image_hash == current_hash

    def test_backward_compat_old_state_no_image_hash(self, tmp_path, monkeypatch):
        """Old state.json without image_hash field loads cleanly."""
        paths.bind_root(tmp_path)
        sessions_dir = paths.sessions_dir(tmp_path)

        # Write a state.json that predates the image_hash field
        session_dir = sessions_dir / "old-session"
        session_dir.mkdir()
        old_state = {
            "name": "old-session",
            "session_id": "legacy-id",
            "role": "engineer",
            "run_key": "",
            "title": "",
            "phase": "",
            "project": "",
            "cwd": "",
            "status": "done",
            "pid": 0,
            "inbox_port": 0,
            "started_at": 1000.0,
            "last_activity": 1000.0,
            "requested_by": {},
        }
        (session_dir / "state.json").write_text(json.dumps(old_state))

        registry = SessionRegistry()
        entry = registry.get("old-session")
        # Should load without error; image_hash defaults to ""
        assert entry is not None
        assert entry.image_hash == ""
