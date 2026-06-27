"""Tests for the in-repo dogfood-content-review agent pack.

Verifies the pack structure, installability, and email event routing.
The dogfood-content-review pack is the canonical exercise of "4th service,
zero framework edits" — email has no adapter, so the monitor injects
events directly into the manager's event queue.
"""

from pathlib import Path

import yaml

from bobi import paths
from bobi.cli import _install_pack
from bobi.config import Config


PACK_DIR = Path(__file__).parent.parent / "agents" / "dogfood-content-review"


class TestPackStructure:
    """The pack has all required files and a valid agent.yaml."""

    def test_agent_yaml_exists(self):
        assert (PACK_DIR / "agent.yaml").exists()

    def test_agent_yaml_parses(self):
        cfg = yaml.safe_load((PACK_DIR / "agent.yaml").read_text())
        assert cfg["entry_point"] == "manager"
        assert cfg["version"] == "1.2.0"

    def test_agent_md_exists(self):
        assert (PACK_DIR / "agent.md").exists()

    def test_all_roles_present(self):
        expected_roles = {"manager", "researcher", "editor", "fact_checker"}
        actual_roles = {
            d.name for d in (PACK_DIR / "roles").iterdir()
            if d.is_dir() and (d / "ROLE.md").exists()
        }
        assert actual_roles == expected_roles

    def test_workflows_present(self):
        expected = {"content-lifecycle.yaml", "dogfood-content-review.yaml",
                    "research-task.yaml", "smoke-test.yaml"}
        actual = {f.name for f in (PACK_DIR / "workflows").iterdir() if f.is_file()}
        assert actual == expected

    def test_tool_guides_present(self):
        assert (PACK_DIR / "tools" / "github_issues.md").exists()

    def test_workspace_fixture_content(self):
        assert (PACK_DIR / "workspace" / "guides" / "getting-started.md").exists()
        assert (PACK_DIR / "workspace" / "runbooks" / "incident-response.md").exists()
        assert (PACK_DIR / "workspace" / "runbooks" / "adding-a-new-repo.md").exists()


class TestInstall:
    """The pack installs cleanly into a project directory."""

    def test_install_succeeds(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        installed = paths.agent_yaml_path(tmp_path)
        assert installed.exists()
        cfg = yaml.safe_load(installed.read_text())
        assert cfg["agent"] == "dogfood-content-review"
        assert cfg["entry_point"] == "manager"

    def test_install_copies_all_roles(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        roles_dir = paths.roles_dir(tmp_path)
        for role in ["manager", "researcher", "editor", "fact_checker"]:
            assert (roles_dir / role / "ROLE.md").exists()

    def test_install_copies_workflows(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        wf_dir = paths.workflows_dir(tmp_path)
        assert (wf_dir / "content-lifecycle.yaml").exists()
        assert (wf_dir / "smoke-test.yaml").exists()

    def test_install_seeds_workspace(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        assert (paths.workspace_dir(tmp_path) / "guides" / "getting-started.md").exists()
        assert (paths.workspace_dir(tmp_path) / "runbooks" / "incident-response.md").exists()

    def test_config_loads_after_install(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        assert cfg.agent == "dogfood-content-review"
        assert cfg.entry_point == "manager"


class TestEmailEventRouting:
    """Email is the "4th service" — no adapter, monitor-polled.

    The pack declares email with events: true so the CLI subscribes to
    the monitor's event topic (email/received). Email has no webhook
    adapter, so the detector falls back to subscription key "email".
    The monitor injects events directly into the manager's event queue.
    """

    def test_email_service_declared_with_events(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        email_svc = next((s for s in cfg.services if s.name == "email"), None)
        assert email_svc is not None, "email service must be declared"
        assert email_svc.events is True, "email events: true drives subscription discovery"

    def test_github_has_events_enabled(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        github_svc = next((s for s in cfg.services if s.name == "github"), None)
        assert github_svc is not None
        assert github_svc.events is True

    def test_email_monitor_exists_with_event_topic(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        monitors = cfg.monitors
        email_mon = next((m for m in monitors if m["name"] == "new-emails"), None)
        assert email_mon is not None, "new-emails monitor must exist"
        assert email_mon["event"] == "email/received"

    def test_email_in_event_services(self, tmp_path):
        """email appears in event_services (drives subscription discovery)."""
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        event_svc_names = [s.name for s in cfg.event_services]
        assert "email" in event_svc_names
        assert "github" in event_svc_names

    def test_email_has_no_registered_adapter(self):
        """email has no adapter — detector falls back to bare service name."""
        from bobi.events.adapters import is_registered
        assert not is_registered("email")

    def test_email_is_venn_service(self, tmp_path):
        """email has no adapter — it should be classified as a Venn service."""
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        venn_names = [s.name for s in cfg.venn_services]
        assert "email" in venn_names

    def test_monitor_event_parts_parse(self):
        """The monitor's event: email/received splits into source=email, type=received."""
        from bobi.monitors.schema import Monitor
        record = Monitor(
            name="new-emails",
            command="venn exec gmail list_messages",
            interval=300,
            event="email/received",
        )
        source, etype = record.event_parts
        assert source == "email"
        assert etype == "received"


class TestContentDirs:
    """content_dirs points to workspace-relative paths that exist after install."""

    def test_content_dirs_reference_workspace(self):
        cfg = yaml.safe_load((PACK_DIR / "agent.yaml").read_text())
        dirs = cfg["context"]["content_dirs"]
        for d in dirs.split():
            assert d.startswith("workspace/"), f"{d} should be under workspace/"

    def test_content_dirs_exist_after_install(self, tmp_path):
        _install_pack(PACK_DIR, tmp_path)
        cfg = yaml.safe_load(paths.agent_yaml_path(tmp_path).read_text())
        dirs = cfg["context"]["content_dirs"]
        for d in dirs.split():
            assert (tmp_path / d).is_dir(), f"{d} should exist after install"


class TestRegistryEntry:
    """The dogfood-content-review pack has an entry in agents/registry.yaml."""

    def test_registry_includes_content_review(self):
        registry_path = Path(__file__).parent.parent / "agents" / "registry.yaml"
        registry = yaml.safe_load(registry_path.read_text())
        assert "dogfood-content-review" in registry["agents"]
        entry = registry["agents"]["dogfood-content-review"]
        assert entry["version"] == "1.2.0"
        assert "description" in entry
