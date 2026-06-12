"""Deterministic worktree cleanup — maps a branch to its worktree and removes both.

Used by the pr-closed workflow's native action step to clean up after a PR
is merged or closed. No LLM involvement — pure git operations.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def cleanup_worktree(repo_root: str, head_branch: str) -> dict:
    """Remove the worktree(s) for a PR's head branch and delete the branch.

    Looks up worktrees by branch name via ``git worktree list --porcelain``
    so it works regardless of where the worktree directory lives on disk.

    Returns a status dict:
      - ``{"status": "cleaned", "paths_removed": [...], "branch": ...}``
      - ``{"status": "not_found"}`` if no worktree matched
    """
    root = Path(repo_root).resolve()
    worktree_paths = _find_worktrees_for_branch(str(root), head_branch)

    if not worktree_paths:
        # No worktree found — try prune + branch delete anyway (stale entry)
        _prune(str(root))
        if _branch_exists(str(root), head_branch):
            _delete_branch(str(root), head_branch)
            return {"status": "cleaned", "paths_removed": [], "branch": head_branch}
        return {"status": "not_found"}

    removed: list[str] = []
    errors: list[str] = []

    for wt_path in worktree_paths:
        ok = _remove_worktree(str(root), wt_path)
        if ok:
            removed.append(wt_path)
        else:
            errors.append(f"Failed to remove {wt_path}")

    _prune(str(root))
    _delete_branch(str(root), head_branch)

    result: dict = {"status": "cleaned", "paths_removed": removed, "branch": head_branch}
    if errors:
        result["errors"] = errors
    return result


def _find_worktrees_for_branch(repo_root: str, branch: str) -> list[str]:
    """Parse ``git worktree list --porcelain`` to find worktree paths on the given branch."""
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error(f"git worktree list failed: {e}")
        return []

    if out.returncode != 0:
        log.error(f"git worktree list failed: {out.stderr.strip()}")
        return []

    paths: list[str] = []
    current_path = ""
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            wt_branch = line[len("branch refs/heads/"):]
            if wt_branch == branch and current_path:
                paths.append(current_path)
            current_path = ""
        elif line == "":
            current_path = ""

    return paths


def _remove_worktree(repo_root: str, worktree_path: str) -> bool:
    """Run ``git worktree remove --force`` on a worktree path."""
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info(f"Removed worktree at {worktree_path}")
            return True
        log.warning(f"git worktree remove failed for {worktree_path}: {result.stderr.strip()}")
        return False
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error(f"git worktree remove failed for {worktree_path}: {e}")
        return False


def _prune(repo_root: str) -> None:
    """Run ``git worktree prune`` to clean up stale admin entries."""
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _branch_exists(repo_root: str, branch: str) -> bool:
    """Check if a local branch exists."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _delete_branch(repo_root: str, branch: str) -> bool:
    """Delete a local branch. Returns True if deleted or already gone."""
    try:
        result = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info(f"Deleted branch {branch}")
            return True
        # Branch might already be gone
        if "not found" in result.stderr.lower():
            return True
        log.warning(f"git branch -D failed for {branch}: {result.stderr.strip()}")
        return False
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error(f"git branch -D failed for {branch}: {e}")
        return False
