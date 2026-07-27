"""Tests for the in-repo dogfood-content-review agent pack.

Verifies the pack structure, installability, and email event routing.
The dogfood-content-review pack is the canonical exercise of "4th service,
zero framework edits" — email has no adapter, so the monitor injects
events directly into the manager's event queue.
"""

import re
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
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(cfg["version"])), cfg["version"]

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


class TestManagerRoutingTable:
    """Every route the manager is told to take must exist in the pack.

    The routing table is the manager's dispatch contract. A row naming a
    workflow the pack does not ship (D060) sends the manager at a `-w` target
    that cannot resolve; a row naming an event surface the pack never
    subscribes to (D119) can never fire at all.
    """

    ROLE_MD = PACK_DIR / "roles" / "manager" / "ROLE.md"

    def _rows(self) -> list[tuple[str, str]]:
        rows = []
        for line in self.ROLE_MD.read_text().splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != 2 or set(cells[0]) <= {"-", ":"}:
                continue
            if cells[0].lower().startswith("event"):
                continue  # header
            rows.append((cells[0], cells[1]))
        return rows

    def test_routing_table_is_parsed(self):
        assert len(self._rows()) >= 4, "manager ROLE.md must ship a routing table"

    def test_every_referenced_target_exists(self):
        import re

        workflows = {p.stem for p in (PACK_DIR / "workflows").glob("*.yaml")}
        roles = {d.name for d in (PACK_DIR / "roles").iterdir() if d.is_dir()}
        for event, action in self._rows():
            for token in re.findall(r"`([^`]+)`", action):
                assert token in workflows or token in roles, (
                    f"routing row {event!r} points at {token!r}, which is "
                    f"neither a workflow ({sorted(workflows)}) nor a role "
                    f"({sorted(roles)})"
                )

    def test_every_routed_event_source_is_declared(self, tmp_path):
        """No row may route an event source the pack never subscribes to."""
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        declared = {s.name for s in cfg.event_services}
        known_sources = {
            "slack", "discord", "whatsapp", "linear", "email", "github",
        }
        for event, _action in self._rows():
            for source in known_sources:
                if source in event.lower():
                    assert source in declared, (
                        f"routing row {event!r} routes {source} events, but the "
                        f"pack declares no {source} event service "
                        f"(declared: {sorted(declared)})"
                    )

    def test_chat_surface_matches_a_declared_service(self, tmp_path):
        """`chat:` must name a declared event service (or `cli` for none)."""
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        if not cfg.chat or cfg.chat == "cli":
            return
        assert cfg.chat in {s.name for s in cfg.event_services}, (
            f"chat: {cfg.chat} but no {cfg.chat} service with events: true is "
            f"declared, so the chat surface can never deliver a message"
        )

    def test_this_pack_ships_with_no_chat_surface_at_all(self, tmp_path):
        """The END STATE D119 resolved to, asserted rather than skipped past.

        The check above early-exits for `cli`/absent, so for the value this
        pack actually ships it asserts nothing: a change to `chat: github` -
        a declared event service, but not a chat transport - would sail
        through, and so would re-adding a chat service and the credential it
        drags back into the release-smoke pack. This one names the intent:
        credential-free, no chat surface.
        """
        _install_pack(PACK_DIR, tmp_path)
        cfg = Config.load(tmp_path)
        assert cfg.chat in (None, "", "cli"), (
            f"chat: {cfg.chat} - the dogfood pack is the credential-free "
            f"release-smoke team and must declare no chat surface"
        )
        chat_transports = {"slack", "telegram", "discord", "whatsapp"}
        declared = {s.name for s in cfg.event_services}
        assert not (declared & chat_transports), (
            f"a chat transport crept back into the pack: "
            f"{sorted(declared & chat_transports)}"
        )


class TestReviewWorkflowRouting:
    """dogfood-content-review must route to `fix` when the audit finds issues.

    The route condition is evaluated against the same VariableContext the
    orchestrator builds: the audit step's handoff values are flattened into
    bare names (D015).
    """

    WORKFLOW = PACK_DIR / "workflows" / "dogfood-content-review.yaml"

    def _route_target(self, issues_count) -> str:
        from bobi.workflow.schema import load_workflow
        from bobi.workflow.variables import VariableContext

        workflow = load_workflow(self.WORKFLOW)
        step = workflow.step_by_name("route_fixes")
        assert step is not None and step.condition

        ctx = VariableContext()
        ctx.set_scope("input", {"task": "review the guide", "repo": "x/y"})
        # Mirrors orchestrator: the audit handoff is set_flat'ed key by key.
        outputs = {"verdict": "needs work", "issues_count": issues_count}
        ctx.set_scope("audit", outputs)
        for key, value in outputs.items():
            ctx.set_flat(key, value)

        return step.goto if ctx.evaluate_condition(step.condition) else step.else_goto

    def test_multiple_issues_route_to_fix(self):
        assert self._route_target(3) == "fix", (
            "an audit reporting 3 issues must reach the fix step"
        )

    def test_single_issue_routes_to_fix(self):
        assert self._route_target(1) == "fix"

    def test_no_issues_routes_to_done(self):
        assert self._route_target(0) == "done"

    def test_string_valued_count_routes_the_same(self):
        """Handoff values arrive as strings from the brain."""
        assert self._route_target("2") == "fix"
        assert self._route_target("0") == "done"


class TestRegistryEntry:
    """The dogfood-content-review pack has an entry in agents/registry.yaml."""

    def test_registry_includes_content_review(self):
        registry_path = Path(__file__).parent.parent / "agents" / "registry.yaml"
        registry = yaml.safe_load(registry_path.read_text())
        assert "dogfood-content-review" in registry["agents"]
        entry = registry["agents"]["dogfood-content-review"]
        assert "description" in entry

    def test_registry_version_matches_the_pack(self):
        """The registry version and the pack's own must agree.

        Pinning a literal here (`== "1.2.1"`) was worse than no check at all:
        it passed while the pack's content changed underneath an unbumped
        version - the failure this is meant to catch - and FAILED the moment
        someone did the right thing and bumped. What is load-bearing is that
        the two agree, since the registry version is what `fetch()` resolves to
        an immutable release asset; a mismatch ships one pack's content under
        another's tarball.
        """
        registry_path = Path(__file__).parent.parent / "agents" / "registry.yaml"
        registry = yaml.safe_load(registry_path.read_text())
        cfg = yaml.safe_load((PACK_DIR / "agent.yaml").read_text())
        assert (registry["agents"]["dogfood-content-review"]["version"]
                == str(cfg["version"]))
