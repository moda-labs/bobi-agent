"""Integration tests for pack context/ and workspace/ install semantics.

Drives the real `bobi install` CLI against a pack shipping both
folders. context/ is part of the frozen image (.bobi/context/,
manifest-tracked); workspace/ seeds <project>/workspace/ once and
reinstall never overwrites user edits.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Run subprocesses against this checkout, not whatever copy of bobi
# happens to be pip-installed in the venv.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


@pytest.fixture
def project_with_pack(tmp_path):
    """A standalone project dir plus a pack with context/ and workspace/."""
    project = tmp_path / "project"
    project.mkdir()

    pack = tmp_path / "research-team"
    (pack / "roles" / "manager").mkdir(parents=True)
    (pack / "roles" / "manager" / "ROLE.md").write_text(
        "# Manager\n\nTest manager.\n")
    (pack / "context").mkdir()
    (pack / "context" / "style-guide.md").write_text(
        "# House style guide\n\nWrite tersely.\n")
    (pack / "workspace").mkdir()
    (pack / "workspace" / "domain-context.md").write_text(
        "# Domain context\n\nFill me in.\n")
    (pack / "workspace" / "briefs").mkdir()
    (pack / "workspace" / "briefs" / ".gitkeep").write_text("")
    (pack / "agent.yaml").write_text(yaml.dump({
        "version": "1.0.0",
        "entry_point": "manager",
    }))
    return project, pack


def _install(project, pack):
    return subprocess.run(
        [sys.executable, "-m", "bobi.cli", "install", str(pack)],
        capture_output=True, text=True, timeout=60, cwd=str(project),
        env=_ENV,
    )


class TestInstallContextWorkspace:

    def test_install_reports_context_and_workspace(self, project_with_pack):
        project, pack = project_with_pack
        result = _install(project, pack)
        assert result.returncode == 0, result.stderr
        assert "context: style-guide.md" in result.stdout
        assert "workspace: seeded to workspace/" in result.stdout

    def test_context_frozen_workspace_seeded(self, project_with_pack):
        project, pack = project_with_pack
        result = _install(project, pack)
        assert result.returncode == 0, result.stderr

        context_file = project / ".bobi" / "context" / "style-guide.md"
        assert context_file.read_text().startswith("# House style guide")
        manifest = json.loads(
            (project / ".bobi" / "install-manifest.json").read_text())
        assert "context/style-guide.md" in manifest["files"]

        seeded = project / "workspace" / "domain-context.md"
        assert seeded.read_text().startswith("# Domain context")
        assert (project / "workspace" / "briefs").is_dir()
        assert not any(p.startswith("workspace") for p in manifest["files"])

    def test_reinstall_restores_context_but_keeps_workspace(
            self, project_with_pack):
        project, pack = project_with_pack
        assert _install(project, pack).returncode == 0

        context_file = project / ".bobi" / "context" / "style-guide.md"
        context_file.write_text("hand-edited\n")
        seeded = project / "workspace" / "domain-context.md"
        seeded.write_text("user filled this in\n")

        assert _install(project, pack).returncode == 0
        assert context_file.read_text().startswith("# House style guide")
        assert seeded.read_text() == "user filled this in\n"

    def test_agent_prompt_indexes_context_and_workspace(
            self, project_with_pack):
        project, pack = project_with_pack
        assert _install(project, pack).returncode == 0

        code = (
            "from bobi.prompts.resolver import resolve_agent_prompt;"
            f"print(resolve_agent_prompt('manager', {str(project)!r}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60, cwd=str(project),
            env=_ENV,
        )
        assert result.returncode == 0, result.stderr
        assert "## Context files" in result.stdout
        assert ".bobi/context/style-guide.md" in result.stdout
        assert "House style guide" in result.stdout
        assert "Write tersely." not in result.stdout
        assert "## Workspace" in result.stdout
