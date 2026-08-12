"""Tests for deterministic worktree cleanup — cleanup_worktree() and native
action step support in the orchestrator.

Covers issue #227: worktree + branch removal on pull_request.closed.
Covers issue #246: cleanup must resolve repo from input, not installation root.
Covers issue #1004: the delete is gated on a LIVE read of the PR's merge
state, and a PR closed without merging keeps its branch and worktree.
"""

import os
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bobi.workflow.schema import StepDef, load_workflow


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
        from bobi.workflow.cleanup import cleanup_worktree

        wt = self._create_worktree(git_repo, "agent/issue-99", "session-99")
        assert wt.exists()

        result = cleanup_worktree(str(git_repo), "agent/issue-99", merged=True)

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
        from bobi.workflow.cleanup import cleanup_worktree

        result = cleanup_worktree(str(git_repo), "agent/nonexistent", merged=True)
        assert result["status"] == "not_found"

    def test_removes_worktree_at_nonstandard_path(self, git_repo):
        """Cleanup finds worktrees regardless of path — it looks up by branch."""
        from bobi.workflow.cleanup import cleanup_worktree

        # Create worktree at a non-standard location (simulating historical mess)
        wt_dir = git_repo.parent / "elsewhere" / "wt-99"
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/old-99", str(wt_dir)],
            cwd=git_repo, capture_output=True, check=True,
        )
        assert wt_dir.exists()

        result = cleanup_worktree(str(git_repo), "agent/old-99", merged=True)
        assert result["status"] == "cleaned"
        assert not wt_dir.exists()

    def test_handles_already_removed_worktree_dir(self, git_repo):
        """If the directory is gone but git still tracks it, prune cleans up."""
        from bobi.workflow.cleanup import cleanup_worktree

        import shutil
        wt = self._create_worktree(git_repo, "agent/ghost-1", "ghost-1")
        # Manually nuke the directory (simulating partial cleanup)
        shutil.rmtree(wt)

        result = cleanup_worktree(str(git_repo), "agent/ghost-1", merged=True)
        # Should succeed — prune handles the stale entry, branch gets deleted
        assert result["status"] == "cleaned"

    def test_preserves_target_checkout_on_head_branch(self, git_repo, tmp_path):
        """The repo_root checkout itself is never removed, even when it is on
        the closed PR branch.

        This protects editable installs where _editable_impl_bobi.pth points at
        the canonical checkout and pr-closed cleanup is asked to operate there.
        """
        from bobi.workflow.cleanup import cleanup_worktree

        checkout = tmp_path / "editable-checkout"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/editable", str(checkout)],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )

        result = cleanup_worktree(str(checkout), "agent/editable", merged=True)

        assert result["status"] == "cleaned"
        assert result["paths_removed"] == []
        assert result["protected_paths"] == [str(checkout)]
        assert checkout.exists()

        current_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
        )
        assert current_branch.stdout.strip() == "agent/editable"

        branches = subprocess.run(
            ["git", "branch", "--list", "agent/editable"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "agent/editable" in branches.stdout


# ---------------------------------------------------------------------------
# Issue #1004 - the merge verdict gates the delete
# ---------------------------------------------------------------------------

class TestCleanupWorktreeMergeGuard:
    """cleanup_worktree deletes nothing unless the caller says the PR merged.

    For a PR closed WITHOUT merging, the head branch is the only copy of the
    work - deleting it destroys it.
    """

    @pytest.fixture
    def git_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "README.md").write_text("init")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        return repo

    def test_unmerged_preserves_worktree_and_branch(self, git_repo):
        from bobi.workflow.cleanup import cleanup_worktree

        wt = git_repo / ".claude" / "worktrees" / "session-1004"
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/issue-1004", str(wt)],
            cwd=git_repo, capture_output=True, check=True,
        )

        result = cleanup_worktree(str(git_repo), "agent/issue-1004", merged=False)

        assert result["status"] == "preserved"
        assert result["branch"] == "agent/issue-1004"
        assert wt.exists()
        branches = subprocess.run(
            ["git", "branch", "--list", "agent/issue-1004"],
            cwd=git_repo, capture_output=True, text=True,
        )
        assert "agent/issue-1004" in branches.stdout

    def test_merged_is_keyword_required(self, git_repo):
        """No call site can delete a branch without stating the verdict."""
        from bobi.workflow.cleanup import cleanup_worktree

        with pytest.raises(TypeError):
            cleanup_worktree(str(git_repo), "agent/issue-1004")

    def test_unmerged_does_not_touch_a_non_git_dir(self, tmp_path):
        """The guard runs before any filesystem probe - preserved, not error."""
        from bobi.workflow.cleanup import cleanup_worktree

        result = cleanup_worktree(str(tmp_path), "agent/issue-1004", merged=False)
        assert result["status"] == "preserved"


class TestPrMergeState:
    """pr_merge_state reads the PR's merge state from GitHub, right now."""

    def _response(self, payload):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        return resp

    def test_reads_merged_true(self):
        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.http.get") as mock_get:
            mock_get.return_value = self._response(
                {"number": 7, "state": "closed", "merged": True}
            )
            state = pr_merge_state("org/repo", 7)

        assert state == {"merged": True, "state": "closed"}
        url = mock_get.call_args[0][0]
        assert url == "https://api.github.com/repos/org/repo/pulls/7"

    def test_reads_merged_false(self):
        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.http.get") as mock_get:
            mock_get.return_value = self._response(
                {"number": 7, "state": "closed", "merged": False}
            )
            assert pr_merge_state("org/repo", "7") == {
                "merged": False, "state": "closed",
            }

    def test_http_failure_reports_error_and_no_verdict(self):
        """An unreadable PR yields no merge verdict - never a default of True."""
        import httpx

        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.http.get", side_effect=httpx.ConnectError("boom")):
            state = pr_merge_state("org/repo", 7)

        assert "merged" not in state
        assert "boom" in state["error"]

    @pytest.mark.parametrize("slug", [
        "not-a-slug",       # no owner/name split
        "",                 # nothing at all
        "org/repo/extra",   # too many segments
        "/repo",            # empty owner
        "org/",             # empty name
        "org/..",           # would walk the API path once httpx normalizes it
        "../repo",
        "org/re po",        # whitespace
    ])
    def test_unusable_slug_reports_error(self, slug):
        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.http.get") as mock_get:
            state = pr_merge_state(slug, 7)

        assert "merged" not in state, f"{slug!r} must not yield a verdict"
        assert "error" in state
        mock_get.assert_not_called()

    def test_unusable_pr_number_reports_error(self):
        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.http.get") as mock_get:
            state = pr_merge_state("org/repo", "?")

        assert "merged" not in state
        assert "error" in state
        mock_get.assert_not_called()

    def test_sends_the_token_when_one_is_available(self):
        from bobi.workflow.cleanup import pr_merge_state

        with patch("bobi.gitutil.github_token", return_value="tok-abc"), \
             patch("bobi.http.get") as mock_get:
            mock_get.return_value = self._response({"merged": True, "state": "closed"})
            pr_merge_state("org/repo", 7)

        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "token tok-abc"


class TestCleanupActionLiveMergeRead:
    """_cleanup_worktree_action takes its verdict from a LIVE read of the PR,
    not from the event payload that started the run."""

    def _ctx(self, **input_overrides):
        from bobi.workflow.variables import VariableContext

        ctx = VariableContext()
        ctx.set_scope("input", {
            "repo": "org/testrepo",
            "pr_number": 1004,
            "head_branch": "agent/issue-1004",
            **input_overrides,
        })
        return ctx

    @pytest.fixture
    def install_root(self, tmp_path):
        child = tmp_path / "testrepo"
        child.mkdir()
        subprocess.run(["git", "init"], cwd=child, capture_output=True)
        return tmp_path

    def test_stale_payload_saying_merged_does_not_authorize_the_delete(
        self, install_root,
    ):
        """The webhook snapshot said merged; GitHub says otherwise right now."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action

        ctx = self._ctx(merged=True)
        with patch("bobi.paths.bobi_root", return_value=install_root), \
             patch("bobi.workflow.cleanup.pr_merge_state") as mock_state:
            mock_state.return_value = {"merged": False, "state": "closed"}
            result = _cleanup_worktree_action(ctx, str(install_root))

        mock_state.assert_called_once_with("org/testrepo", "1004")
        assert result["status"] == "preserved"
        assert result["merged_live"] is False

    def test_live_merged_authorizes_the_delete(self, install_root):
        from bobi.workflow.orchestrator import _cleanup_worktree_action

        ctx = self._ctx(merged=False)
        with patch("bobi.paths.bobi_root", return_value=install_root), \
             patch("bobi.workflow.cleanup.pr_merge_state") as mock_state, \
             patch("bobi.workflow.cleanup.cleanup_worktree") as mock_cleanup:
            mock_state.return_value = {"merged": True, "state": "closed"}
            mock_cleanup.return_value = {
                "status": "cleaned", "paths_removed": [], "branch": "agent/issue-1004",
            }
            result = _cleanup_worktree_action(ctx, str(install_root))

        assert mock_cleanup.call_args[1]["merged"] is True
        assert result["merged_live"] is True

    def test_unreadable_merge_state_preserves(self, install_root):
        """Fail closed: a verdict we could not read is not a licence to delete."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action

        ctx = self._ctx(merged=True)
        with patch("bobi.paths.bobi_root", return_value=install_root), \
             patch("bobi.workflow.cleanup.pr_merge_state") as mock_state:
            mock_state.return_value = {"error": "connection refused"}
            result = _cleanup_worktree_action(ctx, str(install_root))

        assert result["status"] == "preserved"
        assert result["merged_live"] is False
        assert result["merge_state_error"] == "connection refused"
        # The human-facing reason must not claim the PR did not merge - we
        # could not read it at all, which is a different statement.
        assert "could not read" in result["reason"]
        assert "connection refused" in result["reason"]

    def test_missing_head_branch_reads_no_verdict(self):
        """Nothing to delete - do not spend a GitHub call finding that out."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action
        from bobi.workflow.variables import VariableContext

        ctx = VariableContext()
        ctx.set_scope("input", {"repo": "org/testrepo", "pr_number": 1004})
        with patch("bobi.workflow.cleanup.pr_merge_state") as mock_state:
            result = _cleanup_worktree_action(ctx, "/tmp")

        mock_state.assert_not_called()
        assert result["status"] == "skipped"
        assert result["merged_live"] is False

    def test_every_outcome_carries_a_merged_live_verdict(self, install_root):
        """`merged_live` is what the workflow routes on - it must always be set,
        or an unresolvable repo would route as if the PR had merged."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action

        ctx = self._ctx(repo="org/nonexistent")
        with patch("bobi.paths.bobi_root", return_value=install_root):
            result = _cleanup_worktree_action(ctx, str(install_root))

        assert result["status"] == "error"
        assert result["merged_live"] is False


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

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_pr_closed_merged(self, mock_launch):
        from bobi.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-42"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=True)

        assert reactor.process(event) == "dispatched"
        mock_launch.assert_called_once()
        kwargs = mock_launch.call_args[1]
        assert kwargs["workflow_name"] == "pr-closed"
        assert "merged=True" in kwargs["task"] or "merged" in kwargs["task"].lower()

    @patch("bobi.subagent.launch_agent")
    def test_dispatches_pr_closed_unmerged(self, mock_launch):
        from bobi.events.reactor import AutoDispatchRule, EventReactor

        mock_launch.return_value = "wf-pr-closed-test-43"
        rules = [AutoDispatchRule(
            event="github.pull_request",
            workflow="pr-closed",
            match={"action": "closed"},
            cooldown=60,
        )]
        reactor = EventReactor(rules=rules, cwd="/tmp/project")
        event = self._make_pr_closed_event(merged=False, number=43)

        assert reactor.process(event) == "dispatched"
        kwargs = mock_launch.call_args[1]
        assert "merged=False" in kwargs["task"] or "merged" in kwargs["task"].lower()

    @patch("bobi.subagent.launch_agent")
    def test_no_dispatch_on_pr_opened(self, mock_launch):
        from bobi.events.reactor import AutoDispatchRule, EventReactor

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

        assert reactor.process(event) is None
        mock_launch.assert_not_called()

    @patch("bobi.subagent.launch_agent")
    def test_dispatch_passes_event_fields(self, mock_launch):
        """The dispatched task must include fields needed by the workflow
        (merged, head_branch, number, repo)."""
        from bobi.events.reactor import AutoDispatchRule, EventReactor

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

    @patch("bobi.subagent.launch_agent")
    def test_dispatch_passes_input_fields_for_workflow_variables(self, mock_launch):
        """Event fields must be passed as input_fields so the workflow can
        resolve ${{ input.merged }}, ${{ input.head_branch }}, etc."""
        from bobi.events.reactor import AutoDispatchRule, EventReactor

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

    WF_PATH = (
        Path(__file__).parent.parent
        / "agents" / "eng-team" / "workflows" / "pr-closed.yaml"
    )

    def _workflow(self):
        if not self.WF_PATH.exists():
            pytest.skip("pr-closed.yaml not yet created")
        return load_workflow(self.WF_PATH)

    def test_pr_closed_workflow_loads(self):
        wf = self._workflow()
        assert wf.name == "pr-closed"
        # Must have a cleanup step with action
        cleanup = wf.step_by_name("cleanup")
        assert cleanup is not None
        assert cleanup.action == "cleanup_worktree"

    def test_routes_on_the_live_verdict_not_the_event_payload(self):
        """`input.merged` is the webhook's snapshot; `merged_live` is what the
        cleanup step actually read from GitHub. Routing on the snapshot is the
        #1004 defect."""
        wf = self._workflow()
        conditions = [s.condition for s in wf.steps if s.condition]

        assert conditions, "pr-closed must still route on the merge verdict"
        for condition in conditions:
            assert "merged_live" in condition
            assert "input.merged" not in condition

    def test_no_step_closes_the_issue_without_the_merge_verdict(self):
        """close-issue must sit behind a route, never on the fall-through path."""
        wf = self._workflow()
        close_issue = wf.step_by_name("close-issue")

        assert close_issue is not None
        assert wf.steps[wf.step_index("close-issue") - 1].condition, (
            "close-issue must be preceded by the route that guards it"
        )


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


# ---------------------------------------------------------------------------
# Issue #246 — cleanup must not run against installation root
# ---------------------------------------------------------------------------

class TestCleanupWorktreeRejectsNonGitDir:
    """cleanup_worktree must return status:error when repo_root is not a git
    repository, rather than falling through to prune/delete-branch against
    the wrong directory."""

    def test_non_git_dir_returns_error(self, tmp_path):
        from bobi.workflow.cleanup import cleanup_worktree

        # tmp_path is a plain directory, not a git repo
        result = cleanup_worktree(str(tmp_path), "agent/issue-99", merged=True)
        assert result["status"] == "error"
        assert "not a git repository" in result["reason"]


class TestResolveRepoRoot:
    """_resolve_repo_root must find the local checkout from input.repo,
    not fall back to the installation root."""

    def _make_ctx(self, repo_slug: str) -> "VariableContext":
        from bobi.workflow.variables import VariableContext

        ctx = VariableContext()
        ctx.set_scope("input", {"repo": repo_slug, "head_branch": "agent/x"})
        return ctx

    def test_director_layout_finds_child_repo(self, tmp_path):
        """When the installation root contains a child dir matching the repo
        name and that child is a git repo, resolve to it."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        # Set up: installation root with a child git repo
        child = tmp_path / "myrepo"
        child.mkdir()
        subprocess.run(["git", "init"], cwd=child, capture_output=True)

        ctx = self._make_ctx("org/myrepo")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result == str(child)

    def test_single_repo_layout(self, tmp_path):
        """When the installation root itself is a git repo whose remote
        matches the slug, resolve to it."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/org/somerepo.git"],
            cwd=tmp_path,
            capture_output=True,
        )

        ctx = self._make_ctx("org/somerepo")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result == str(tmp_path)

    def test_single_repo_slug_mismatch_returns_none(self, tmp_path):
        """When the installation root is a git repo but its remote does NOT
        match the event's slug, return None — do not run git ops against
        the wrong repo.  Regression: a same-named branch must survive."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        # Installation root is a git repo for a *different* project
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/org/install-repo.git"],
            cwd=tmp_path,
            capture_output=True,
        )
        # Need a commit before we can create branches
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        # Create a branch with the same name the cleanup would target
        subprocess.run(
            ["git", "checkout", "-b", "agent/x"],
            cwd=tmp_path,
            capture_output=True,
        )

        ctx = self._make_ctx("org/some-other-repo")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result is None

        # The branch must still exist — no git ops should have run
        branches = subprocess.run(
            ["git", "branch", "--list", "agent/x"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "agent/x" in branches.stdout

    def test_path_traversal_rejected(self, tmp_path):
        """A crafted input.repo with '..' must not escape the install root."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        # Create a parent dir that happens to be a git repo
        parent = tmp_path / "parent"
        parent.mkdir()
        install = parent / "install"
        install.mkdir()
        subprocess.run(["git", "init"], cwd=parent, capture_output=True)

        ctx = self._make_ctx("org/..")
        with patch("bobi.paths.bobi_root", return_value=install):
            result = _resolve_repo_root(ctx)

        assert result is None

    def test_substring_slug_does_not_match(self, tmp_path):
        """A slug like 'org/api' must NOT match a remote for 'org/api-private'."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/org/api-private.git"],
            cwd=tmp_path,
            capture_output=True,
        )

        ctx = self._make_ctx("org/api")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result is None

    def test_ssh_remote_url_matches(self, tmp_path):
        """SSH remote URLs (git@github.com:org/repo.git) must match."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:org/somerepo.git"],
            cwd=tmp_path,
            capture_output=True,
        )

        ctx = self._make_ctx("org/somerepo")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result == str(tmp_path)

    def test_no_repo_found_returns_none(self, tmp_path):
        """When no matching checkout exists, return None."""
        from bobi.workflow.orchestrator import _resolve_repo_root

        ctx = self._make_ctx("org/missing")
        with patch("bobi.paths.bobi_root", return_value=tmp_path):
            result = _resolve_repo_root(ctx)

        assert result is None

    def test_no_repo_in_input_returns_none(self):
        """When input.repo is missing, return None."""
        from bobi.workflow.orchestrator import _resolve_repo_root
        from bobi.workflow.variables import VariableContext

        ctx = VariableContext()
        ctx.set_scope("input", {"head_branch": "agent/x"})
        with patch("bobi.paths.bobi_root"):
            result = _resolve_repo_root(ctx)

        assert result is None


class TestCleanupActionUsesInputRepo:
    """_cleanup_worktree_action must resolve repo from input.repo,
    not from _find_project_root. Issue #246."""

    def test_action_resolves_repo_from_input(self, tmp_path):
        """The action should call cleanup_worktree with the resolved repo
        path, not the installation root."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action
        from bobi.workflow.variables import VariableContext

        # Set up a child git repo under a non-git installation root
        child = tmp_path / "testrepo"
        child.mkdir()
        subprocess.run(["git", "init"], cwd=child, capture_output=True)

        ctx = VariableContext()
        ctx.set_scope("input", {
            "repo": "org/testrepo",
            "pr_number": 246,
            "head_branch": "agent/issue-246",
        })

        with patch("bobi.paths.bobi_root", return_value=tmp_path), \
             patch("bobi.workflow.cleanup.pr_merge_state",
                   return_value={"merged": True, "state": "closed"}), \
             patch("bobi.workflow.cleanup.cleanup_worktree") as mock_cleanup:
            mock_cleanup.return_value = {"status": "cleaned", "paths_removed": [], "branch": "agent/issue-246"}
            result = _cleanup_worktree_action(ctx, str(tmp_path))

        # cleanup_worktree must be called with the child repo, not tmp_path
        mock_cleanup.assert_called_once_with(
            str(child), "agent/issue-246", merged=True,
        )
        assert result["status"] == "cleaned"

    def test_action_returns_error_when_repo_unresolvable(self):
        """When input.repo can't be resolved to a local checkout, return error."""
        from bobi.workflow.orchestrator import _cleanup_worktree_action
        from bobi.workflow.variables import VariableContext

        ctx = VariableContext()
        ctx.set_scope("input", {
            "repo": "org/nonexistent",
            "head_branch": "agent/issue-246",
        })

        with patch("bobi.paths.bobi_root", return_value=Path("/tmp/empty")):
            result = _cleanup_worktree_action(ctx, "/tmp/empty")

        assert result["status"] == "error"
        assert "could not resolve" in result["reason"]
