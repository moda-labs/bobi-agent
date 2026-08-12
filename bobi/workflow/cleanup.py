"""Deterministic worktree cleanup — maps a branch to its worktree and removes both.

Used by the pr-closed workflow's native action step to clean up after a PR is
merged or closed. No LLM involvement - git operations, plus the one GitHub
read that decides whether any of them may run at all.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# GitHub owner and repository names: letters, digits, and -._ after an
# alphanumeric first character. Requiring that first character is what excludes
# "." and "..", so a crafted slug cannot walk the API path.
_REPO_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def pr_merge_state(repo_slug: str, pr_number: str | int) -> dict:
    """Read a pull request's merge state from GitHub, right now.

    The webhook payload that starts a pr-closed run is a snapshot of the
    moment the hook fired; by the time the run reaches cleanup it says
    nothing about the repo's state now. This is the live answer.

    Returns ``{"merged": bool, "state": str}`` on a successful read, or
    ``{"error": "..."}`` when the read could not be made at all. It never
    raises, and it never guesses a verdict - an unreadable PR yields no
    ``merged`` key, so a caller cannot mistake "could not tell" for "merged".
    """
    # Both halves must be real path segments. The host is fixed, so a bad slug
    # cannot reach another server, but "org/.." would still resolve to a
    # different api.github.com endpoint once httpx normalizes the path.
    owner, _, name = str(repo_slug).partition("/")
    if not _REPO_SEGMENT.match(owner) or not _REPO_SEGMENT.match(name):
        return {"error": f"unusable repo slug: {repo_slug!r}"}
    number = str(pr_number).strip()
    if not number.isdigit():
        return {"error": f"unusable PR number: {pr_number!r}"}
    repo_slug = f"{owner}/{name}"

    from bobi import http as pooled
    from bobi.gitutil import github_token

    url = f"{GITHUB_API}/repos/{repo_slug}/pulls/{number}"
    headers = {"Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        resp = pooled.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning(
            "Could not read merge state for %s#%s: %s", repo_slug, number, e,
        )
        return {"error": f"{type(e).__name__}: {e}"}

    if not isinstance(data, dict):
        return {"error": f"unexpected PR payload for {repo_slug}#{number}"}
    return {"merged": data.get("merged") is True, "state": str(data.get("state", ""))}


def cleanup_worktree(repo_root: str, head_branch: str, *, merged: bool) -> dict:
    """Remove the worktree(s) for a PR's head branch and delete the branch.

    Looks up worktrees by branch name via ``git worktree list --porcelain``
    so it works regardless of where the worktree directory lives on disk.

    ``merged`` is the caller's verdict on whether the PR actually merged, and
    it is keyword-REQUIRED for the same reason the deletion exists at all: a
    merged PR's head is a redundant copy, and an unmerged PR's head is the
    only copy of the work. Making it a required keyword means no call site can
    reach the delete without having answered the question (#1004).

    Returns a status dict:
      - ``{"status": "preserved", "branch": ...}`` if ``merged`` is false -
        nothing was inspected, nothing was removed
      - ``{"status": "cleaned", "paths_removed": [...], "branch": ...}``
      - ``{"status": "not_found"}`` if no worktree matched
    """
    if not merged:
        log.info(
            "Preserving worktree and branch %s: the PR did not merge",
            head_branch,
        )
        return {
            "status": "preserved",
            "branch": head_branch,
            "reason": "PR is not merged - branch and worktree left in place",
        }

    root = Path(repo_root).resolve()

    # Fail loudly if repo_root is not a git repository — running worktree
    # prune / branch -D against a non-repo (e.g. the installation root) is
    # destructive and wrong.  See issue #246.
    if not (root / ".git").exists():
        log.error("cleanup_worktree called on non-git directory: %s", root)
        return {"status": "error", "reason": f"not a git repository: {root}"}

    worktree_paths = _find_worktrees_for_branch(str(root), head_branch)

    if not worktree_paths:
        # No worktree found — try prune + branch delete anyway (stale entry)
        _prune(str(root))
        if _branch_exists(str(root), head_branch):
            _delete_branch(str(root), head_branch)
            return {"status": "cleaned", "paths_removed": [], "branch": head_branch,
                    "reason": "deleted the branch; no worktree was registered for it"}
        return {"status": "not_found",
                "reason": f"no worktree or branch named {head_branch}"}

    removed: list[str] = []
    protected: list[str] = []
    errors: list[str] = []

    for wt_path in worktree_paths:
        if _is_target_checkout(str(root), wt_path):
            log.warning("Skipping cleanup for target checkout: %s", wt_path)
            protected.append(wt_path)
            continue
        ok = _remove_worktree(str(root), wt_path)
        if ok:
            removed.append(wt_path)
        else:
            errors.append(f"Failed to remove {wt_path}")

    _prune(str(root))
    if not protected:
        _delete_branch(str(root), head_branch)

    result: dict = {
        "status": "cleaned",
        "paths_removed": removed,
        "branch": head_branch,
        "reason": f"removed {len(removed)} worktree(s)" + (
            "; kept the checkout cleanup was run from" if protected
            else " and deleted the branch"
        ),
    }
    if protected:
        result["protected_paths"] = protected
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


def _is_target_checkout(repo_root: str, worktree_path: str) -> bool:
    """Return True when a matching worktree is the checkout being cleaned from."""
    try:
        return Path(worktree_path).resolve() == Path(repo_root).resolve()
    except OSError:
        return False


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
