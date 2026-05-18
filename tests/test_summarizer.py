"""Tests for the summarizer's phase detection.

Uses real git operations on tmp directories to test that detect_phase
correctly identifies worktree state.
"""

import subprocess
from pathlib import Path

from dispatch.summarizer import detect_phase


def _git(cwd, *args):
    subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, check=True)


def _init_repo_with_worktree(tmp_path):
    """Create a git repo with a main branch and a worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("# Test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    wt = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "agent/test-1", str(wt))
    return repo, wt


class TestDetectPhase:

    def test_empty_worktree_is_starting(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        result = detect_phase(str(wt))
        assert result["phase"] == "starting"
        assert not result["has_commits"]

    def test_dispatch_dir_only_is_triage_complete(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / ".dispatch").mkdir()
        (wt / ".dispatch" / "handoff.md").write_text("test")
        result = detect_phase(str(wt))
        assert result["phase"] == "triage_complete"

    def test_spec_file_is_spec_complete(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / "specs").mkdir()
        (wt / "specs" / "test-1-feature.md").write_text("# Spec\n")
        _git(wt, "add", "specs/")
        _git(wt, "commit", "-m", "spec")
        result = detect_phase(str(wt))
        assert result["phase"] == "spec_complete"
        assert result["spec_path"] == "specs/test-1-feature.md"

    def test_code_changes_is_implementation_complete(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / "app.py").write_text("print('hello')\n")
        _git(wt, "add", "app.py")
        _git(wt, "commit", "-m", "implement")
        result = detect_phase(str(wt))
        assert result["phase"] == "implementation_complete"
        assert result["has_commits"]

    def test_spec_plus_code_is_implementation_complete(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / "specs").mkdir()
        (wt / "specs" / "test-1.md").write_text("# Spec\n")
        _git(wt, "add", "specs/")
        _git(wt, "commit", "-m", "spec")

        (wt / "app.py").write_text("print('hello')\n")
        _git(wt, "add", "app.py")
        _git(wt, "commit", "-m", "implement")

        result = detect_phase(str(wt))
        assert result["phase"] == "implementation_complete"
        assert result["spec_path"] == "specs/test-1.md"

    def test_dispatch_only_commits_is_triage(self, tmp_path):
        """Commits that only touch .dispatch/ should be triage, not implementation."""
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / ".dispatch").mkdir()
        (wt / ".dispatch" / "handoff.md").write_text("test")
        _git(wt, "add", ".dispatch/")
        _git(wt, "commit", "-m", "triage handoff")
        result = detect_phase(str(wt))
        # No non-spec, non-dispatch changes = triage
        assert result["phase"] == "triage_complete"


class TestDetectPhaseFields:

    def test_has_commits_true_when_committed(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        (wt / "test.py").write_text("pass\n")
        _git(wt, "add", "test.py")
        _git(wt, "commit", "-m", "test")
        assert detect_phase(str(wt))["has_commits"] is True

    def test_has_commits_false_when_empty(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        assert detect_phase(str(wt))["has_commits"] is False

    def test_summary_not_empty(self, tmp_path):
        _, wt = _init_repo_with_worktree(tmp_path)
        result = detect_phase(str(wt))
        assert len(result["summary"]) > 0
