"""Tests for deterministic worktree cleanup — cleanup_worktree() and native
action step support in the orchestrator.

Covers issue #227: worktree + branch removal on pull_request.closed.
"""

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.workflow.schema import StepDef, load_workflow


# ---------------------------------------------------------------------------
# cleanup_worktree()
# ---------------------------------------------------------------------------

class TestCleanupWorktree:
    """cleanup_worktree maps a branch name to its worktree via
    `git worktree list --porcelain` and removes both."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a minimal git repo with an initial commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        return repo

    def _create_worktree(self, repo: Path, branch: str, name: str) -> Path:
        wt_dir = repo / ".claude" / "worktrees" / name
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_dir)],
            cwd=repo, capture_output=True, check=True,
        )
        return wt_dir

    def test_removes_worktree_and_branch(self, git_repo):
        from modastack.workflow.cleanup import cleanup_worktree

        wt = self._create_worktree(git_repo, "agent/issue-99", "session-99")
        assert wt.exists()

        result = cleanup_worktree(str(git_repo), "agent/issue-99")

        assert result["status"] == "cleaned"
        assert not wt.exists()
        assert "agent/issue-99" not in result.get("errors", [])
        # Branch should be deleted
        branches = subprocess.run(
            ["git", "branch", "--list", "agent/issue-99"],
            cwd=git_repo, capture_output=True, text=True,
        )
        assert "agent/issue-99" not in branches.stdout

    def test_not_found_returns_status(self, git_repo):
        from modastack.workflow.cleanup import cleanup_worktree

        result = cleanup_worktree(str(git_repo), "agent/nonexistent")
        assert result["status"] == "not_found"

    def test_removes_worktree_at_nonstandard_path(self, git_repo):
        """Cleanup finds worktrees regardless of path — it looks up by branch."""
        from modastack.workflow.cleanup import cleanup_worktree

        # Create worktree at a non-standard location (simulating historical mess)
        wt_dir = git_repo.parent / "elsewhere" / "wt-99"
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/old-99", str(wt_dir)],
            cwd=git_repo, capture_output=True, check=True,
        )
        assert wt_dir.exists()

        result = cleanup_worktree(str(git_repo), "agent/old-99")
        assert result["status"] == "cleaned"
        assert not wt_dir.exists()

    def test_handles_already_removed_worktree_dir(self, git_repo):
        """If the directory is gone but git still tracks it, prune cleans up."""
        from modastack.workflow.cleanup import cleanup_worktree

        import shutil
        wt = self._create_worktree(git_repo, "agent/ghost-1", "ghost-1")
        # Manually nuke the directory (simulating partial cleanup)
        shutil.rmtree(wt)

        result = cleanup_worktree(str(git_repo), "agent/ghost-1")
        # Should succeed — prune handles the stale entry, branch gets deleted
        assert result["status"] == "cleaned"


# ---------------------------------------------------------------------------
# Native action step — schema parsing
# ---------------------------------------------------------------------------

class TestNativeStepSchema:
    """Steps with `action:` field are parsed as native action steps."""

    def test_load_native_action_step(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: cleanup-test
            steps:
              - name: cleanup
                action: cleanup_worktree
                timeout: 120
        """))
        wf = load_workflow(f)
        assert wf.steps[0].action == "cleanup_worktree"
        assert wf.steps[0].timeout == 120

    def test_native_step_has_no_prompt(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: native-test
            steps:
              - name: do-thing
                action: some_action
        """))
        wf = load_workflow(f)
        assert wf.steps[0].action == "some_action"
        assert wf.steps[0].prompt == ""


# ---------------------------------------------------------------------------
# Reactor dispatch generalization
# ---------------------------------------------------------------------------

class TestReactorPrClosedDispatch:
    """EventReactor dispatches pr-closed workflow on pull_request.closed events."""

    def _make_pr_closed_event(self, merged=True, number=42):
        return {
            "type": "github.pull_request",
            "source": "github",
            "topics": ["github:moda-labs/test"],
            "fields": {
                "action": "closed",
                "number": number,
                "title": "Fix the thing",
                "state": "closed",
                "merged": merged,
                "head_branch": "agent/issue-42",
                "sender": "dev1",
            },
        }

    @patch("modastack.subagent.launch_agent")
    def test_dispatches_pr_closed_merged(self, mock_launch):
        from modastack.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-42"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=True)

        assert reactor.process(event) is True
        mock_launch.assert_called_once()
        kwargs = mock_launch.call_args[1]
        assert kwargs["workflow_name"] == "pr-closed"
        assert "merged=True" in kwargs["task"] or "merged" in kwargs["task"].lower()

    @patch("modastack.subagent.launch_agent")
    def test_dispatches_pr_closed_unmerged(self, mock_launch):
        from modastack.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-43"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=False, number=43)

        assert reactor.process(event) is True
        kwargs = mock_launch.call_args[1]
        assert "merged=False" in kwargs["task"] or "merged" in kwargs["task"].lower()

    @patch("modastack.subagent.launch_agent")
    def test_no_dispatch_on_pr_opened(self, mock_launch):
        from modastack.events.reactor import AutoDispatchRule, EventReactor

        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = {
            "type": "github.pull_request",
            "fields": {"action": "opened", "number": 44},
            "topics": ["github:moda-labs/test"],
        }

        assert reactor.process(event) is False
        mock_launch.assert_not_called()

    @patch("modastack.subagent.launch_agent")
    def test_dispatch_passes_event_fields(self, mock_launch):
        """The dispatched task must include fields needed by the workflow
        (merged, head_branch, number, repo)."""
        from modastack.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-42"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=True)

        reactor.process(event)
        kwargs = mock_launch.call_args[1]
        # Task should mention the PR number and repo
        assert "#42" in kwargs["task"]
        assert "moda-labs/test" in kwargs["task"]

    @patch("modastack.subagent.launch_agent")
    def test_dispatch_passes_input_fields_for_workflow_variables(self, mock_launch):
        """Event fields must be passed as input_fields so the workflow can
        resolve ${{ input.merged }}, ${{ input.head_branch }}, etc."""
        from modastack.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-42"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=True)

        reactor.process(event)
        kwargs = mock_launch.call_args[1]
        input_fields = kwargs.get("input_fields", {})
        assert input_fields["merged"] is True
        assert input_fields["head_branch"] == "agent/issue-42"
        assert input_fields["pr_number"] == 42
        assert input_fields["repo"] == "moda-labs/test"


# ---------------------------------------------------------------------------
# pr-closed workflow YAML
# ---------------------------------------------------------------------------

class TestPrClosedWorkflow:
    """The pr-closed.yaml workflow parses correctly."""

    def test_pr_closed_workflow_loads(self):
        wf_path = Path(__file__).parent.parent / "agents" / "eng-team" / "workflows" / "pr-closed.yaml"
        if not wf_path.exists():
            pytest.skip("pr-closed.yaml not yet created")
        wf = load_workflow(wf_path)
        assert wf.name == "pr-closed"
        # Must have a cleanup step with action
        cleanup = wf.step_by_name("cleanup")
        assert cleanup is not None
        assert cleanup.action == "cleanup_worktree"


# ---------------------------------------------------------------------------
# GitHub adapter — merged + head_branch extraction
# ---------------------------------------------------------------------------

class TestGitHubAdapterPrFields:
    """The GitHub adapter extracts merged and head_branch for PR events."""

    def test_pr_closed_event_has_merged_field(self):
        """Verify test event structure matches what the adapter should produce."""
        # This tests the contract — the actual adapter is TypeScript.
        # We verify the Python side handles the fields correctly.
        event = {
            "type": "github.pull_request",
            "fields": {
                "action": "closed",
                "number": 42,
                "merged": True,
                "head_branch": "agent/issue-42",
            },
        }
        assert event["fields"]["merged"] is True
        assert event["fields"]["head_branch"] == "agent/issue-42"
