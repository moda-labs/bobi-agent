"""Tests for frozen-image pack install.

run/package/ mimics a runtime installation: install regenerates it
verbatim from the pack source — no merge with prior installed state.
Variance enters via ${VAR}/.env only. The install manifest lets doctor
flag hand-edits before a reinstall silently destroys them.
"""

import json
import os

import pytest
import yaml
from click.testing import CliRunner

from bobi import paths
from bobi.cli import _install_pack, _write_install_gitignore, main
from bobi.config import parse_env_file
from bobi.doctor import _check_install_integrity


@pytest.fixture(autouse=True)
def _clear_bound_root():
    paths.bind_root(None)
    yield
    paths.bind_root(None)


@pytest.fixture
def pack(tmp_path):
    pack_dir = tmp_path / "agents" / "my-team"
    (pack_dir / "roles" / "manager").mkdir(parents=True)
    (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
    (pack_dir / "workflows").mkdir()
    (pack_dir / "workflows" / "adhoc.yaml").write_text("steps: []\n")
    pack_dir.joinpath("agent.yaml").write_text(
        "version: '1.0'\nentry_point: manager\nevent_server: ${BOBI_EVENT_SERVER}\n"
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
    cfg = yaml.safe_load((paths.agent_yaml_path(project)).read_text())
    assert cfg["entry_point"] == "manager"
    assert cfg["event_server"] == "${BOBI_EVENT_SERVER}"
    assert cfg["agent"] == "my-team"


def test_reinstall_discards_hand_edits(pack, project):
    """The installed image is frozen — reinstall restores pack content."""
    _install_pack(pack, project)
    installed = paths.agent_yaml_path(project)
    cfg = yaml.safe_load(installed.read_text())
    cfg["entry_point"] = "director"
    cfg["subscribe"] = ["github:o/r"]
    installed.write_text(yaml.dump(cfg))
    (paths.roles_dir(project) / "manager" / "ROLE.md").write_text("edited\n")

    _install_pack(pack, project)

    cfg = yaml.safe_load(installed.read_text())
    assert cfg["entry_point"] == "manager"
    assert "subscribe" not in cfg
    role = paths.roles_dir(project) / "manager" / "ROLE.md"
    assert role.read_text() == "# Manager\n"


def test_install_is_idempotent(pack, project):
    _install_pack(pack, project)
    first = (paths.agent_yaml_path(project)).read_text()
    manifest_first = (paths.install_manifest_path(project)).read_text()
    _install_pack(pack, project)
    assert (paths.agent_yaml_path(project)).read_text() == first
    assert (paths.install_manifest_path(project)).read_text() == manifest_first


def test_manifest_covers_installed_files(pack, project):
    _install_pack(pack, project)
    manifest = json.loads(
        (paths.install_manifest_path(project)).read_text())
    assert manifest["agent"] == "my-team"
    assert manifest["frozen"] is True
    assert "agent.yaml" in manifest["files"]
    assert "roles/manager/ROLE.md" in manifest["files"]
    assert "workflows/adhoc.yaml" in manifest["files"]


def test_local_source_gitignore_covers_image(pack, project):
    _install_pack(pack, project)
    _write_install_gitignore(project, local_source=True)
    entries = (paths.package_dir(project) / ".gitignore").read_text().splitlines()
    for artifact in ["agent.yaml", "agent.md", "roles/", "install-manifest.json",
                     ".gitignore"]:
        assert artifact in entries
    _write_install_gitignore(project, local_source=False)
    entries = (paths.package_dir(project) / ".gitignore").read_text().splitlines()
    assert "agent.yaml" not in entries
    assert "install-manifest.json" in entries


class TestContextInstall:
    """context/ is part of the frozen image: pack-owned, read-only reference."""

    def test_context_installs_to_dot_bobi(self, pack, project):
        _install_pack(pack, project)
        installed = paths.context_dir(project) / "style-guide.md"
        assert installed.read_text().startswith("# House style guide")

    def test_reinstall_restores_context_edits(self, pack, project):
        _install_pack(pack, project)
        installed = paths.context_dir(project) / "style-guide.md"
        installed.write_text("edited\n")
        _install_pack(pack, project)
        assert installed.read_text().startswith("# House style guide")

    def test_context_in_manifest_and_gitignore(self, pack, project):
        _install_pack(pack, project)
        manifest = json.loads(
            (paths.install_manifest_path(project)).read_text())
        assert "context/style-guide.md" in manifest["files"]
        _write_install_gitignore(project, local_source=True)
        entries = (paths.package_dir(project) / ".gitignore").read_text().splitlines()
        assert "context/" in entries


class TestWorkspaceSeed:
    """workspace/ is user-owned: seeded once at the project root, never
    overwritten by reinstall, never tracked in the manifest."""

    def test_workspace_seeds_to_project_root(self, pack, project):
        _install_pack(pack, project)
        seeded = paths.workspace_dir(project) / "domain-context.md"
        assert seeded.read_text().startswith("# Domain context")
        assert (paths.workspace_dir(project) / "briefs").is_dir()

    def test_reinstall_preserves_workspace_edits(self, pack, project):
        _install_pack(pack, project)
        seeded = paths.workspace_dir(project) / "domain-context.md"
        seeded.write_text("user filled this in\n")
        _install_pack(pack, project)
        assert seeded.read_text() == "user filled this in\n"

    def test_reinstall_seeds_only_missing_files(self, pack, project):
        _install_pack(pack, project)
        (paths.workspace_dir(project) / "domain-context.md").unlink()
        (pack / "workspace" / "new-template.md").write_text("new\n")
        _install_pack(pack, project)
        assert (paths.workspace_dir(project) / "domain-context.md").exists()
        assert (paths.workspace_dir(project) / "new-template.md").read_text() == "new\n"

    def test_workspace_not_in_manifest(self, pack, project):
        _install_pack(pack, project)
        manifest = json.loads(
            (paths.install_manifest_path(project)).read_text())
        assert not any(p.startswith("workspace") for p in manifest["files"])

    def test_pack_without_workspace_seeds_nothing(self, pack, project):
        import shutil
        shutil.rmtree(pack / "workspace")
        _install_pack(pack, project)
        assert not (paths.workspace_dir(project)).exists()


class TestPromptSections:
    """The resolver indexes context files and points at workspace/."""

    def test_context_index_lists_files_with_descriptions(self, pack, project):
        from bobi.prompts.resolver import resolve_agent_prompt
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Context files" in prompt
        assert "`package/context/style-guide.md` — House style guide" in prompt
        # Index only — contents are read on demand, never inlined.
        assert "Write tersely." not in prompt

    def test_workspace_note_present_when_seeded(self, pack, project):
        from bobi.prompts.resolver import resolve_agent_prompt
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Workspace" in prompt
        assert f"`{paths.workspace_dir(project)}`" in prompt

    def test_sections_absent_without_context_or_workspace(self, pack, project):
        import shutil
        from bobi.prompts.resolver import resolve_agent_prompt
        shutil.rmtree(pack / "context")
        shutil.rmtree(pack / "workspace")
        _install_pack(pack, project)
        prompt = resolve_agent_prompt("manager", project)
        assert "## Context files" not in prompt
        assert "## Workspace" not in prompt


class TestDoctorIntegrity:

    def _set_root(self, project, monkeypatch):
        paths.bind_root(None)
        paths.bind_root(project)

    def test_clean_install_passes(self, pack, project, monkeypatch):
        _install_pack(pack, project)
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert result.ok
        assert "frozen, clean" in result.detail

    def test_drift_is_flagged(self, pack, project, monkeypatch):
        _install_pack(pack, project)
        (paths.agent_yaml_path(project)).write_text("agent: edited\n")
        (paths.workflows_dir(project) / "adhoc.yaml").unlink()
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert not result.ok
        assert "2 file(s)" in result.detail
        assert "reinstall" in result.hint

    def test_downloaded_pack_is_editable(self, pack, project, monkeypatch):
        _install_pack(pack, project, local_source=False)
        (paths.agent_yaml_path(project)).write_text("agent: edited\n")
        self._set_root(project, monkeypatch)
        result = _check_install_integrity()
        assert result.ok
        assert "editable" in result.detail

    def test_no_manifest_is_ok(self, project, monkeypatch):
        paths.package_dir(project).mkdir(parents=True)
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
            "event_server: ${BOBI_EVENT_SERVER}\n"
            "api_key: ${MY_API_KEY}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# my-team\n")
        return pack_dir

    def test_env_vars_written_to_dotenv(self, pack_with_creds, tmp_path, monkeypatch):
        """With --non-interactive, env vars present in os.environ are
        written to run/.env without prompting."""
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.setenv("BOBI_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.setenv("MY_API_KEY", "sk-test-123")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_with_creds), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        env = parse_env_file(home / "agents" / "my-team" / "run" / ".env")
        assert env["BOBI_EVENT_SERVER"] == "wss://events.example.com"
        assert env["MY_API_KEY"] == "sk-test-123"

    def test_missing_required_secret_fails_fast(self, pack_with_creds, tmp_path, monkeypatch):
        """--non-interactive must never prompt, and a missing REQUIRED secret
        (a bare ${VAR}) must fail fast with a non-zero exit and a clear message
        — never exit 0 into a broken start. (Completing without hanging also
        proves it never blocked on input.)"""
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.setenv("BOBI_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_with_creds), "--non-interactive"],
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
            "event_server: ${BOBI_EVENT_SERVER}\n"
            "model: ${OPTIONAL_MODEL:-sonnet}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.setenv("BOBI_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("OPTIONAL_MODEL", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_dir), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        env = parse_env_file(home / "agents" / "opt-team" / "run" / ".env")
        assert env["BOBI_EVENT_SERVER"] == "wss://events.example.com"

    def test_existing_env_file_preserved(self, pack_with_creds, tmp_path, monkeypatch):
        """Vars already in .env are kept; env vars supplement them."""
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        env_file = home / "agents" / "my-team" / "run" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("MY_API_KEY=existing-key\n")

        monkeypatch.setenv("BOBI_EVENT_SERVER", "wss://events.example.com")
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_with_creds), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        env = parse_env_file(env_file)
        assert env["MY_API_KEY"] == "existing-key"
        assert env["BOBI_EVENT_SERVER"] == "wss://events.example.com"

    def test_optional_var_from_environment_written_by_name(self, tmp_path, monkeypatch):
        """A SET ${VAR:-default} value is copied from the environment into
        .env under its plain name (regression: the raw 'VAR:-default' token
        used to be looked up verbatim, so set optional vars were never
        captured)."""
        pack_dir = tmp_path / "agents" / "opt-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "event_server: ${BOBI_EVENT_SERVER:-}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.setenv("BOBI_EVENT_SERVER", "wss://events.example.com")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_dir), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        env = parse_env_file(home / "agents" / "opt-team" / "run" / ".env")
        assert env["BOBI_EVENT_SERVER"] == "wss://events.example.com"

    def test_optional_missing_warns_by_plain_name(self, tmp_path, monkeypatch):
        """The optional-missing warning names the var, not the raw
        'VAR:-default' token."""
        pack_dir = tmp_path / "agents" / "opt-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "model: ${OPTIONAL_MODEL:-sonnet}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.delenv("OPTIONAL_MODEL", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_dir), "--non-interactive"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "OPTIONAL_MODEL" in result.output
        assert "OPTIONAL_MODEL:-" not in result.output

    def test_interactive_default_still_prompts(self, pack_with_creds, tmp_path, monkeypatch):
        """Without --non-interactive, install still prompts (baseline)."""
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.delenv("BOBI_EVENT_SERVER", raising=False)
        monkeypatch.delenv("MY_API_KEY", raising=False)

        runner = CliRunner()
        # Provide empty input to satisfy prompts without hanging.
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_with_creds)],
            input="\n\n",
        )
        assert result.exit_code == 0
        assert "credentials" in result.output.lower()

    def test_interactive_event_server_prompt_hints_blank_is_local(
            self, tmp_path, monkeypatch):
        """The BOBI_EVENT_SERVER prompt says blank = the auto-started local
        server, and an entered value for a ${VAR:-} ref is stored under its
        plain name."""
        pack_dir = tmp_path / "agents" / "opt-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "event_server: ${BOBI_EVENT_SERVER:-}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.delenv("BOBI_EVENT_SERVER", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_dir)],
            input="wss://events.example.com\n",
        )
        assert result.exit_code == 0, result.output
        assert "leave blank to auto-start the local server" in result.output
        env = parse_env_file(home / "agents" / "opt-team" / "run" / ".env")
        assert env["BOBI_EVENT_SERVER"] == "wss://events.example.com"

    def test_interactive_event_server_blank_stays_unset(
            self, tmp_path, monkeypatch):
        """Accepting the blank default leaves BOBI_EVENT_SERVER out of .env,
        so start auto-launches the local event server."""
        pack_dir = tmp_path / "agents" / "opt-team"
        (pack_dir / "roles" / "manager").mkdir(parents=True)
        (pack_dir / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")
        pack_dir.joinpath("agent.yaml").write_text(
            "version: '1.0'\n"
            "entry_point: manager\n"
            "event_server: ${BOBI_EVENT_SERVER:-}\n"
        )
        pack_dir.joinpath("agent.md").write_text("# opt-team\n")

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        monkeypatch.delenv("BOBI_EVENT_SERVER", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["agents", "install", str(pack_dir)],
            input="\n",
        )
        assert result.exit_code == 0, result.output
        env = parse_env_file(home / "agents" / "opt-team" / "run" / ".env")
        assert "BOBI_EVENT_SERVER" not in env
