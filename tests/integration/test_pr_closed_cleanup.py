"""Integration coverage for the pr-closed workflow's merge guard (#1004).

The workflow's destructive step used to run unconditionally, with the `merged`
guard sitting a step later where it gated only issue-closing. A guard that runs
after the destructive step cannot protect it.

These tests drive the REAL `agents/eng-team/workflows/pr-closed.yaml` through
the orchestrator, against a real git repo holding a real worktree, and assert
what actually survives on disk. The merge verdict is stubbed at the HTTP
boundary only - everything from the workflow YAML down through the native
action and the git shell-outs is the production path.
"""

import subprocess
from pathlib import Path

import pytest

PR_CLOSED_YAML = (
    Path(__file__).resolve().parents[2]
    / "agents" / "eng-team" / "workflows" / "pr-closed.yaml"
)

REPO_SLUG = "test-org/test-repo"


def _pr_response(payload: dict):
    """A stand-in for the httpx response `bobi.http.get` returns."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _add_worktree(repo: Path, branch: str) -> Path:
    wt = repo / "worktrees" / branch.replace("/", "-")
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt)],
        cwd=repo, capture_output=True, check=True,
    )
    return wt


def _branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo, capture_output=True,
    ).returncode == 0


def _drive_pr_closed(env, monkeypatch, *, branch, pr_number, run_key,
                     live_merged, payload_merged):
    """Run the real pr-closed workflow once; return (result, api_urls_read)."""
    from bobi.workflow.orchestrator import run_workflow
    from bobi.workflow.schema import load_workflow

    urls: list[str] = []

    def _fake_get(url, *, headers=None, timeout=None):
        urls.append(url)
        return _pr_response(
            {"number": pr_number, "state": "closed", "merged": live_merged}
        )

    monkeypatch.setattr("bobi.http.get", _fake_get)

    result = run_workflow(
        load_workflow(PR_CLOSED_YAML),
        task=f"PR #{pr_number} in {REPO_SLUG} was closed",
        repo=REPO_SLUG,
        cwd=str(env.project_path),
        run_key=run_key,
        timeout=60,
        interactive=False,
        input_fields={
            "repo": REPO_SLUG,
            "pr_number": pr_number,
            "head_branch": branch,
            "merged": payload_merged,
        },
    )
    return result, urls


class TestPrClosedMergeGuard:
    """Drive the shipped workflow with a closed PR and check the disk."""

    def test_closed_unmerged_pr_keeps_its_branch_and_worktree(
        self, stub_bobi_env, monkeypatch,
    ):
        """The event payload says merged; GitHub says otherwise right now.

        The live read wins, and nothing is deleted - for an unmerged PR that
        head branch is the only copy of the work.
        """
        repo = stub_bobi_env.project_path
        branch = "agent/issue-1004-unmerged"
        wt = _add_worktree(repo, branch)

        result, urls = _drive_pr_closed(
            stub_bobi_env, monkeypatch,
            branch=branch, pr_number=1004, run_key="1004-unmerged",
            live_merged=False,
            # The stale snapshot the webhook delivered.
            payload_merged=True,
        )

        assert result is True
        assert urls == [f"https://api.github.com/repos/{REPO_SLUG}/pulls/1004"], (
            "the guard must re-read the PR, not trust the event payload"
        )
        assert wt.exists(), "worktree of an unmerged PR was deleted"
        assert _branch_exists(repo, branch), "branch of an unmerged PR was deleted"

    def test_unreadable_merge_state_keeps_its_branch_and_worktree(
        self, stub_bobi_env, monkeypatch,
    ):
        """Fail closed: a verdict we could not read is not a licence to delete."""
        import httpx

        from bobi.workflow.orchestrator import run_workflow
        from bobi.workflow.schema import load_workflow

        repo = stub_bobi_env.project_path
        branch = "agent/issue-1004-unreadable"
        wt = _add_worktree(repo, branch)

        monkeypatch.setattr(
            "bobi.http.get",
            lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no route")),
        )

        result = run_workflow(
            load_workflow(PR_CLOSED_YAML),
            task=f"PR #1005 in {REPO_SLUG} was closed",
            repo=REPO_SLUG,
            cwd=str(repo),
            run_key="1005-unreadable",
            timeout=60,
            interactive=False,
            input_fields={
                "repo": REPO_SLUG,
                "pr_number": 1005,
                "head_branch": branch,
                "merged": True,
            },
        )

        assert result is True
        assert wt.exists()
        assert _branch_exists(repo, branch)

    def test_merged_pr_still_gets_cleaned_up(self, stub_bobi_env, monkeypatch):
        """The positive control: the guard gates the delete, it does not
        disable it. Without this, a permanently-broken action would pass the
        preservation tests above."""
        repo = stub_bobi_env.project_path
        branch = "agent/issue-1004-merged"
        wt = _add_worktree(repo, branch)

        # The run's own outcome is not asserted: on the merged path the
        # workflow goes on to `close-issue`, a prompt step whose handoff the
        # stub brain cannot write. The cleanup this test is about has already
        # happened by then, and it is what gets checked.
        _drive_pr_closed(
            stub_bobi_env, monkeypatch,
            branch=branch, pr_number=1006, run_key="1006-merged",
            live_merged=True, payload_merged=True,
        )

        assert not wt.exists(), "worktree of a merged PR was left behind"
        assert not _branch_exists(repo, branch), "branch of a merged PR was left behind"


class TestPrClosedWorkflowShape:
    """The shipped YAML must keep the guard ahead of the delete."""

    def test_cleanup_is_the_gated_native_action(self):
        from bobi.workflow.schema import load_workflow

        wf = load_workflow(PR_CLOSED_YAML)
        cleanup = wf.step_by_name("cleanup")

        assert cleanup is not None and cleanup.action == "cleanup_worktree"
        # Every route in this workflow keys off the verdict the cleanup step
        # actually read, never the event payload it was launched with.
        for step in wf.steps:
            if step.condition:
                assert "merged_live" in step.condition
                assert "input.merged" not in step.condition
