"""Unit tests for WorkflowDispatcher — format_workflow_menu() and
deduplication by resolution tier (repo > user > default)."""

import textwrap
from pathlib import Path

import pytest

from modastack.workflow.triggers import WorkflowDispatcher
from modastack.workflow.schema import Workflow, StepDef, load_workflow


def _make_workflow_yaml(path: Path, name: str, trigger: str, description: str = ""):
    path.write_text(textwrap.dedent(f"""\
        name: {name}
        trigger: >
          {trigger}
        description: >
          {description or trigger}
        steps:
          - name: work
            prompt: "Do the thing"
    """))


class TestFormatWorkflowMenu:
    def test_menu_contains_all_workflows(self, tmp_path):
        dispatcher = WorkflowDispatcher()
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "a.yaml", "alpha",
                           "When an issue is assigned.",
                           "Handle assigned issues.")
        _make_workflow_yaml(d / "b.yaml", "beta",
                           "When CI fails.",
                           "Fix CI failures.")
        dispatcher._load_from(d, source="default")

        menu = dispatcher.format_workflow_menu()
        assert "alpha" in menu
        assert "beta" in menu
        assert "When an issue is assigned." in menu
        assert "When CI fails." in menu

    def test_menu_includes_description(self, tmp_path):
        dispatcher = WorkflowDispatcher()
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "a.yaml", "lifecycle",
                           "When an issue is assigned and requires code changes.",
                           "Full engineering lifecycle: triage, spec, implement, PR.")
        dispatcher._load_from(d, source="default")

        menu = dispatcher.format_workflow_menu()
        assert "lifecycle" in menu
        assert "Full engineering lifecycle" in menu

    def test_menu_empty_when_no_workflows(self):
        dispatcher = WorkflowDispatcher()
        menu = dispatcher.format_workflow_menu()
        assert "No workflows loaded" in menu

    def test_dedup_repo_overrides_default(self, tmp_path):
        dispatcher = WorkflowDispatcher()

        default_dir = tmp_path / "default"
        default_dir.mkdir()
        _make_workflow_yaml(default_dir / "wf.yaml", "lifecycle",
                           "Default trigger for lifecycle.",
                           "Default description.")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _make_workflow_yaml(repo_dir / "wf.yaml", "lifecycle",
                           "Repo-specific trigger for lifecycle.",
                           "Repo-specific description.")

        dispatcher._load_from(repo_dir, source="/path/to/repo")
        dispatcher._load_from(default_dir, source="default")

        menu = dispatcher.format_workflow_menu()
        assert "Repo-specific trigger" in menu
        assert "Default trigger" not in menu

    def test_dedup_user_overrides_default(self, tmp_path):
        dispatcher = WorkflowDispatcher()

        default_dir = tmp_path / "default"
        default_dir.mkdir()
        _make_workflow_yaml(default_dir / "wf.yaml", "lifecycle",
                           "Default trigger.",
                           "Default desc.")

        user_dir = tmp_path / "user"
        user_dir.mkdir()
        _make_workflow_yaml(user_dir / "wf.yaml", "lifecycle",
                           "User-level trigger.",
                           "User desc.")

        user_dir2 = tmp_path / "user2"
        user_dir2.mkdir()

        dispatcher._load_from(user_dir, source="user")
        dispatcher._load_from(default_dir, source="default")

        menu = dispatcher.format_workflow_menu()
        assert "User-level trigger" in menu
        assert "Default trigger" not in menu

    def test_dedup_repo_overrides_user(self, tmp_path):
        dispatcher = WorkflowDispatcher()

        user_dir = tmp_path / "user"
        user_dir.mkdir()
        _make_workflow_yaml(user_dir / "wf.yaml", "lifecycle",
                           "User trigger.",
                           "User desc.")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _make_workflow_yaml(repo_dir / "wf.yaml", "lifecycle",
                           "Repo trigger.",
                           "Repo desc.")

        dispatcher._load_from(repo_dir, source="/path/to/repo")
        dispatcher._load_from(user_dir, source="user")

        menu = dispatcher.format_workflow_menu()
        assert "Repo trigger" in menu
        assert "User trigger" not in menu

    def test_different_names_not_deduped(self, tmp_path):
        dispatcher = WorkflowDispatcher()

        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "a.yaml", "alpha",
                           "When issues are assigned.")
        _make_workflow_yaml(d / "b.yaml", "beta",
                           "When CI fails.")

        dispatcher._load_from(d, source="default")
        menu = dispatcher.format_workflow_menu()
        assert "alpha" in menu
        assert "beta" in menu


class TestFindWorkflow:
    def test_find_by_name(self, tmp_path):
        dispatcher = WorkflowDispatcher()
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "a.yaml", "lifecycle",
                           "When an issue is assigned.")
        _make_workflow_yaml(d / "b.yaml", "adhoc",
                           "For any ad-hoc task.")
        dispatcher._load_from(d, source="default")

        wf = dispatcher.find_workflow("lifecycle")
        assert wf is not None
        assert wf.name == "lifecycle"

    def test_find_missing_returns_none(self, tmp_path):
        dispatcher = WorkflowDispatcher()
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "a.yaml", "lifecycle",
                           "When an issue is assigned.")
        dispatcher._load_from(d, source="default")

        assert dispatcher.find_workflow("nonexistent") is None


class TestLoadFrom:
    def test_loads_yaml_files(self, tmp_path):
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "test.yaml", "test-wf",
                           "When something happens.")
        dispatcher = WorkflowDispatcher()
        dispatcher._load_from(d, source="default")
        assert len(dispatcher.workflows) == 1
        assert dispatcher.workflows[0][0].name == "test-wf"

    def test_skips_nonexistent_directory(self, tmp_path):
        dispatcher = WorkflowDispatcher()
        dispatcher._load_from(tmp_path / "nonexistent", source="default")
        assert len(dispatcher.workflows) == 0

    def test_records_source(self, tmp_path):
        d = tmp_path / "workflows"
        d.mkdir()
        _make_workflow_yaml(d / "test.yaml", "test-wf",
                           "When something happens.")
        dispatcher = WorkflowDispatcher()
        dispatcher._load_from(d, source="my-repo")
        assert dispatcher.workflows[0][1] == "my-repo"


class TestNoFrameworkFallback:
    """Verify workflows resolve only from .modastack/, not from the framework package."""

    def test_no_bundled_workflow_yamls_in_package(self):
        """The modastack/workflow/ directory must not contain any YAML files.

        Domain workflows belong in agent packs, not the framework package.
        """
        package_dir = Path(__file__).parent.parent / "modastack" / "workflow"
        yamls = list(package_dir.glob("*.yaml"))
        assert yamls == [], (
            f"Framework package must not ship workflow YAMLs: {[f.name for f in yamls]}"
        )

    def test_load_all_without_project_raises(self, monkeypatch):
        """Without a project path and without a bound root, loading raises —
        silently loading nothing was the failure mode that dispatched
        engineers with no workflows."""
        import pytest
        monkeypatch.setattr("modastack.paths._root", None)
        dispatcher = WorkflowDispatcher()
        with pytest.raises(RuntimeError, match="not bound"):
            dispatcher.load_all_workflows(project_path=None)

    def test_load_all_uses_only_installed_pack(self, tmp_path):
        """Workflows load exclusively from .modastack/workflows/."""
        wf_dir = tmp_path / ".modastack" / "workflows"
        wf_dir.mkdir(parents=True)
        _make_workflow_yaml(wf_dir / "my-wf.yaml", "my-workflow",
                           "When something custom happens.")

        dispatcher = WorkflowDispatcher()
        dispatcher.load_all_workflows(project_path=tmp_path)
        names = [wf.name for wf, _src in dispatcher.workflows]
        assert names == ["my-workflow"]
