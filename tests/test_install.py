"""Tests for frozen-image pack install.

.modastack/ mimics a runtime installation: install regenerates it
verbatim from the pack source — no merge with prior installed state.
Variance enters via ${VAR}/.env only. The install manifest lets doctor
flag hand-edits before a reinstall silently destroys them.
"""

import json
import os

import pytest
import yaml
from click.testing import CliRunner

from modastack.cli import _install_pack, _write_install_gitignore, main
from modastack.config import parse_env_file
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
    (pack_dir / "context").mkdir()
    (pack_dir / "context" / "style-guide.md").write_text(
        "# House style guide\n\nWrite tersely.\n")
    (pack_dir / "workspace").mkdir()
    (pack_dir / "workspace" / "domain-context.md").write_text(
        "# Domain context\n\nFill me in.\n")
    (pack_dir / "workspace" / "briefs").mkdir()
    (pack_dir / "workspace" / "briefs" / ".gitkeep").write_text("")
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


class TestContextInstall:
    """context/ is part of the frozen image: pack-owned, read-only reference."""

    def test_context_installs_to_dot_modastack(self, pack, project):
        _install_pack(pack, project)
        installed = project / ".modastack" / "context" / "style-guide.md"
        assert installed.read_text().startswith("# House style guide")

    def test_reinstall_restores_context_edits(self, pack, project):
        _install_pack(pack, project)
        installed = project / ".modastack" / "context" / "style-guide.md"
        installed.write_text("edited\n")
        _install_pack(pack, project)
        assert installed.read_text().startswith("# House style guide")

    def test_context_in_manifest_and_gitignore(self, pack, project):
        _install_pack(pack, project)
        manifest = json.loads(
            (project / ".modastack" / "install-manifest.json").read_text())
        assert "context/style-guide.md" in manifest["files"]
        _write_install_gitignore(project, local_source=True)
        entries = (project / ".modastack" / ".gitignore").read_text().splitlines()
        assert "context/" in entries


class TestWorkspaceSeed:
    """workspace/ is user-owned: seeded once at the project root, never
    overwritten by reinstall, never tracked in the manifest."""

    def test_workspace_seeds_to_project_root(self, pack, project):
        _install_pack(pack, project)
        seeded = project / "workspace" / "domain-context.md"
        assert seeded.read_text().startswith("# Domain context")
        assert (project / "workspace" / "briefs").is_dir()

    def test_reinstall_preserves_workspace_edits(self, pack, project):
        _install_pack(pack, project)
        seeded = project / "workspace" / "domain-context.md"
        seeded.write_text("user filled this in\n")
        _install_pack(pack, project)
        assert seeded.read_text() == "user filled this in\n"

    def test_reinstall_seeds_only_missing_files(self, pack, project):
        _install_pack(pack, project)
        (project / "workspace" / "domain-context.md").unlink()
        (pack / "workspace" / "new-template.md").write_text("new\n")
        _install_pack(pack, project)
        assert (project / "workspace" / "domain-context.md").exists()
        assert (project / "workspace" / "new-template.md").read_text() == "new\n"

    def test_workspace_not_in_manifest(self, pack, project):
        _install_pack(pack, project)
        manifest = json.loads(
            (project / ".modastack" / "install-manifest.json").read_text())
        assert not any(p.startswith("workspace") for p in manifest["files"])

    def test_pack_without_workspace_seeds_nothing(self, pack, project):
        import shutil
        shutil.rmtree(pack / "workspace")
        _install_pack(pack, project)
        assert not (project / "workspace").exists()


class TestPromptSections:
    """The resolver indexes context files and points at workspace/."""

    def test_context_index_lists_files_with_descriptions(self, pack, project):
        from modastack.prompts.resolver import resolve_agent_prompt
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Context files" in prompt
        assert "`.modastack/context/style-guide.md` — House style guide" in prompt
        # Index only — contents are read on demand, never inlined.
        assert "Write tersely." not in prompt

    def test_workspace_note_present_when_seeded(self, pack, project):
        from modastack.prompts.resolver import resolve_agent_prompt
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Workspace" in prompt
        assert "`workspace/`" in prompt

    def test_sections_absent_without_context_or_workspace(self, pack, project):
        import shutil
        from modastack.prompts.resolver import resolve_agent_prompt
        shutil.rmtree(pack / "context")
        shutil.rmtree(pack / "workspace")
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Context files" not in prompt
        assert "## Workspace" not in prompt


class TestDoctorIntegrity:

    def _set_root(self, project, monkeypatch):
        monkeypatch.setattr("modastack.paths._root", project)

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


class TestNonInteractiveInstall:
    """--non-interactive skips prompts; secrets come from the environment."""

    @pytest.fixture
    def pack_with_creds(self, tmp_path):
        """A pack whose agent.yaml references two env vars."""
        pack_dir = tmp_path / "agents" / "my-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "event_server: ${MODASTACK_EVENT_SERVER}\n"
            "api_key: ${MY_API_KEY}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# my-team\n")
        return pack_dir

    def test_env_vars_written_to_dotenv(self, pack_with_creds, tmp_path, monkeypatch):
        """With --non-interactive, env vars present in os.environ are
        written to .modastack/.env without prompting."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.setenv("MODASTACK_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.setenv("MY_API_KEY", "sk-test-123")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["install", str(pack_with_creds), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        env = parse_env_file(project / ".modastack" / ".env")
        assert env["MODASTACK_EVENT_SERVER"] == "wss://events.example.com"
        assert env["MY_API_KEY"] == "sk-test-123"

    def test_missing_required_secret_fails_fast(self, pack_with_creds, tmp_path, monkeypatch):
        """--non-interactive must never prompt, and a missing REQUIRED secret
        (a bare ${VAR}) must fail fast with a non-zero exit and a clear message
        — never exit 0 into a broken start. (Completing without hanging also
        proves it never blocked on input.)"""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.setenv("MODASTACK_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["install", str(pack_with_creds), "--non-interactive"],
        )
        assert result.exit_code == 1, result.output
        assert "MY_API_KEY" in result.output
        assert "required secrets missing" in result.output

    def test_missing_optional_var_is_ok(self, tmp_path, monkeypatch):
        """A missing ${VAR:-default} (optional, carries its own fallback) only
        warns and still succeeds — it must not trip the fail-fast guard."""
        pack_dir = tmp_path / "agents" / "opt-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "event_server: ${MODASTACK_EVENT_SERVER}\n"
            "model: ${OPTIONAL_MODEL:-sonnet}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.setenv("MODASTACK_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("OPTIONAL_MODEL", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["install", str(pack_dir), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        env = parse_env_file(project / ".modastack" / ".env")
        assert env["MODASTACK_EVENT_SERVER"] == "wss://events.example.com"

    def test_existing_env_file_preserved(self, pack_with_creds, tmp_path, monkeypatch):
        """Vars already in .env are kept; env vars supplement them."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        dot_moda = project / ".modastack"
        dot_moda.mkdir(parents=True)
        (dot_moda / ".env").write_text("MY_API_KEY=existing-key\n")

        monkeypatch.setenv("MODASTACK_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["install", str(pack_with_creds), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        env = parse_env_file(project / ".modastack" / ".env")
        assert env["MY_API_KEY"] == "existing-key"
        assert env["MODASTACK_EVENT_SERVER"] == "wss://events.example.com"

    def test_interactive_default_still_prompts(self, pack_with_creds, tmp_path, monkeypatch):
        """Without --non-interactive, install still prompts (baseline)."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.delenv("MODASTACK_EVENT_SERVER", raising=False)
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        # Provide empty input to satisfy prompts without hanging.
        result = runner.invoke(
            main,
            ["install", str(pack_with_creds)],
            input="\n\n",
        )
        assert result.exit_code == 0
        assert "credentials" in result.output.lower()
