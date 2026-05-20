"""Inspect worktree state to determine what phase an engineer is in."""

import json
import shutil
import subprocess
from pathlib import Path


def _git(worktree: str, *args) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=worktree, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _gh(worktree: str, *args) -> str:
    gh = shutil.which("gh") or "gh"
    result = subprocess.run(
        [gh] + list(args),
        cwd=worktree, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def detect_phase(worktree: str) -> dict:
    """Determine the current phase from worktree state.

    Returns dict with:
      phase: str
      pr_url: str | None
      spec_path: str | None
      has_commits: bool
      summary: str
    """
    wt = Path(worktree)

    pr_url = None
    pr_merged = False
    pr_raw = _gh(worktree, "pr", "view", "--json", "url,state")
    if pr_raw:
        try:
            pr_data = json.loads(pr_raw)
            if pr_data.get("state") in ("OPEN", "MERGED"):
                pr_url = pr_data.get("url")
            if pr_data.get("state") == "MERGED":
                pr_merged = True
        except (json.JSONDecodeError, ValueError):
            pass

    commit_log = _git(worktree, "log", "--oneline", "main..HEAD")
    has_commits = bool(commit_log)
    changed_files = _git(worktree, "diff", "--name-only", "main..HEAD").splitlines()
    uncommitted = _git(worktree, "diff", "--name-only").splitlines()
    unstaged = _git(worktree, "diff", "--name-only", "--cached").splitlines()
    all_changed = list(set(changed_files + uncommitted + unstaged))

    non_spec = [f for f in all_changed
                if not f.startswith("specs/") and not f.startswith(".dispatch")]
    spec_files = [f for f in all_changed if f.startswith("specs/") and f.endswith(".md")]
    spec_path = spec_files[0] if spec_files else None

    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    is_pushed = bool(_git(worktree, "rev-parse", "--verify", f"origin/{branch}"))

    if pr_merged:
        return {"phase": "done", "pr_url": pr_url, "spec_path": spec_path,
                "has_commits": has_commits, "summary": f"PR merged: {pr_url}"}
    if pr_url:
        return {"phase": "in_review", "pr_url": pr_url, "spec_path": spec_path,
                "has_commits": has_commits, "summary": f"PR created: {pr_url}"}
    if non_spec and is_pushed:
        return {"phase": "implementation_complete", "pr_url": None, "spec_path": spec_path,
                "has_commits": has_commits, "summary": f"Pushed. {len(non_spec)} files changed."}
    if non_spec:
        return {"phase": "implementation_complete", "pr_url": None, "spec_path": spec_path,
                "has_commits": has_commits, "summary": f"Code changes. {len(non_spec)} files."}
    if spec_path:
        return {"phase": "spec_complete", "pr_url": None, "spec_path": spec_path,
                "has_commits": has_commits, "summary": f"Spec: {spec_path}"}
    if has_commits or (wt / ".dispatch").exists():
        return {"phase": "triage_complete", "pr_url": None, "spec_path": None,
                "has_commits": has_commits, "summary": "Triage complete."}
    return {"phase": "starting", "pr_url": None, "spec_path": None,
            "has_commits": False, "summary": "No work yet."}
