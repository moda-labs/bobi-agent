"""Tests for frozen-image pack install.

.modastack/ mimics a runtime installation: install regenerates it
verbatim from the pack source — no merge with prior installed state.
Variance enters via ${VAR}/.env only. The install manifest lets doctor
flag hand-edits before a reinstall silently destroys them.
"""

import json

import pytest
import yaml

from modastack.cli import _install_pack, _write_install_gitignore
from modastack.doctor import _check_install_integrity


@pytest.fixture
def pack(tmp_path):
    pack_dir = tmp_path / "agents" / "my-team"
    (pack_dir / "roles" / "manager").mkdir(parents=True)
    (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
    (pack_dir / "workflows").mkdir()
    (pack_dir / "workflows" / "adhoc.yaml").write_text("steps: []\n")
    pack_dir.joinpath("agent.yaml").write_text(
        "version: '1.0'\nentry_point: manager\nevent_server: ${MODASTACK_EVENT_SERVER}\n"
    )
    pack_dir.joinpath("agent.md").write_text("# my-team\n")
    return pack_dir


@pytest.fixture
def project(tmp_path):
    return tmp_path


def test_install_writes_pack_yaml_verbatim_plus_name(pack, project):
    _install_pack(pack, project)
    cfg = yaml.safe_load((project / ".modastack" / "agent.yaml").read_text())
    assert cfg["entry_point"] == "manager"
    assert cfg["event_server"] == "${MODASTACK_EVENT_SERVER}"
    assert cfg["agent"] == "my-team"


def test_reinstall_discards_hand_edits(pack, project):
    """The installed image is frozen — reinstall restores pack content."""
    _install_pack(pack, project)
    installed = project / ".modastack" / "agent.yaml"
    cfg = yaml.safe_load(installed.read_text())
    cfg["entry_point"] = "director"
    cfg["subscribe"] = ["github:o/r"]
    installed.write_text(yaml.dump(cfg))
    (project / ".modastack" / "roles" / "manager" / "ROLE.md").write_text("edited\n")

    _install_pack(pack, project)

    cfg = yaml.safe_load(installed.read_text())
    assert cfg["entry_point"] == "manager"
    assert "subscribe" not in cfg
    role = project / ".modastack" / "roles" / "manager" / "ROLE.md"
    assert role.read_text() == "# Manager\n"


def test_install_is_idempotent(pack, project):
    _install_pack(pack, project)
    first = (project / ".modastack" / "agent.yaml").read_text()
    manifest_first = (project / ".modastack" / "install-manifest.json").read_text()
    _install_pack(pack, project)
    assert (project / ".modastack" / "agent.yaml").read_text() == first
    assert (project / ".modastack" / "install-manifest.json").read_text() == manifest_first


def test_manifest_covers_installed_files(pack, project):
    _install_pack(pack, project)
    manifest = json.loads(
        (project / ".modastack" / "install-manifest.json").read_text())
    assert manifest["agent"] == "my-team"
    assert manifest["frozen"] is True
    assert "agent.yaml" in manifest["files"]
    assert "roles/manager/ROLE.md" in manifest["files"]
    assert "workflows/adhoc.yaml" in manifest["files"]


def test_local_source_gitignore_covers_image(pack, project):
    _install_pack(pack, project)
    _write_install_gitignore(project, local_source=True)
    entries = (project / ".modastack" / ".gitignore").read_text().splitlines()
    for artifact in ["agent.yaml", "agent.md", "roles/", "install-manifest.json",
                     ".gitignore"]:
        assert artifact in entries
    _write_install_gitignore(project, local_source=False)
    entries = (project / ".modastack" / ".gitignore").read_text().splitlines()
    assert "agent.yaml" not in entries
    assert "install-manifest.json" in entries


class TestDoctorIntegrity:

    def _set_root(self, project, monkeypatch):
        from modastack import sdk
        monkeypatch.setattr(sdk, "get_project_root", lambda: project)

    def test_clean_install_passes(self, pack, project, monkeypatch):
        _install_pack(pack, project)
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert result.ok
        assert "frozen, clean" in result.detail

    def test_drift_is_flagged(self, pack, project, monkeypatch):
        _install_pack(pack, project)
        (project / ".modastack" / "agent.yaml").write_text("agent: edited\n")
        (project / ".modastack" / "workflows" / "adhoc.yaml").unlink()
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert not result.ok
        assert "2 file(s)" in result.detail
        assert "reinstall" in result.hint

    def test_downloaded_pack_is_editable(self, pack, project, monkeypatch):
        _install_pack(pack, project, local_source=False)
        (project / ".modastack" / "agent.yaml").write_text("agent: edited\n")
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert result.ok
        assert "editable" in result.detail

    def test_no_manifest_is_ok(self, project, monkeypatch):
        (project / ".modastack").mkdir()
        self._set_root(project, monkeypatch)
        assert _check_install_integrity().ok
