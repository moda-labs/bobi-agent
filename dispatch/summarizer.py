"""Summarize agent work and write the handoff.

After an agent completes a task and returns to idle, the summarizer:
1. Inspects the worktree (git status, commits, PRs, specs)
2. Captures what the agent did from the tmux pane
3. Determines the correct phase
4. Writes .dispatch/handoff.md

The agent never writes the handoff — this module owns it entirely.
"""

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from .session import capture

log = logging.getLogger(__name__)


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
      summary: str (one-line description of state)
    """
    wt = Path(worktree)

    # Check for PR
    pr_url = None
    pr_raw = _gh(worktree, "pr", "view", "--json", "url,state")
    if pr_raw:
        try:
            pr_data = json.loads(pr_raw)
            if pr_data.get("state") in ("OPEN", "MERGED"):
                pr_url = pr_data.get("url")
        except (json.JSONDecodeError, ValueError):
            pass

    # Check for commits beyond main and what changed
    commit_log = _git(worktree, "log", "--oneline", "main..HEAD")
    has_commits = bool(commit_log)

    changed_files = _git(worktree, "diff", "--name-only", "main..HEAD").splitlines()

    # Also check uncommitted changes (agent may have edited but not committed)
    uncommitted = _git(worktree, "diff", "--name-only").splitlines()
    unstaged = _git(worktree, "diff", "--name-only", "--cached").splitlines()
    all_changed = list(set(changed_files + uncommitted + unstaged))

    non_spec_changes = [f for f in all_changed
                        if not f.startswith("specs/") and not f.startswith(".dispatch")]

    # Check for spec files created on this branch (not inherited from main)
    spec_path = None
    spec_files_in_diff = [f for f in all_changed if f.startswith("specs/") and f.endswith(".md")]
    if spec_files_in_diff:
        spec_path = spec_files_in_diff[0]

    # Check for pushed branch
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    push_status = _git(worktree, "rev-parse", "--verify", f"origin/{branch}")
    is_pushed = bool(push_status)

    # Determine phase
    if pr_url:
        return {
            "phase": "in_review",
            "pr_url": pr_url,
            "spec_path": spec_path,
            "has_commits": has_commits,
            "summary": f"PR created: {pr_url}",
        }

    if non_spec_changes and is_pushed:
        return {
            "phase": "implementation_complete",
            "pr_url": None,
            "spec_path": spec_path,
            "has_commits": has_commits,
            "summary": f"Implementation pushed. {len(non_spec_changes)} files changed.",
        }

    if non_spec_changes:
        return {
            "phase": "implementation_complete",
            "pr_url": None,
            "spec_path": spec_path,
            "has_commits": has_commits,
            "summary": f"Implementation committed (not pushed). {len(non_spec_changes)} files changed.",
        }

    if spec_path:
        return {
            "phase": "spec_complete",
            "pr_url": None,
            "spec_path": spec_path,
            "has_commits": has_commits,
            "summary": f"Spec written: {spec_path}",
        }

    if has_commits:
        return {
            "phase": "triage_complete",
            "pr_url": None,
            "spec_path": None,
            "has_commits": True,
            "summary": "Triage committed.",
        }

    # Worktree exists but no commits — still triaging
    if (wt / ".dispatch").exists():
        return {
            "phase": "triage_complete",
            "pr_url": None,
            "spec_path": None,
            "has_commits": False,
            "summary": "Triage complete, no commits yet.",
        }

    return {
        "phase": "starting",
        "pr_url": None,
        "spec_path": None,
        "has_commits": False,
        "summary": "No work detected yet.",
    }


def summarize_pane(issue_id: str) -> str:
    """Extract a brief summary of what the agent did from the tmux pane."""
    raw = capture(issue_id, lines=80)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # Filter out UI chrome (box drawing, prompts, status bars)
    content_lines = []
    for line in lines:
        if line.startswith("─") or line.startswith("━"):
            continue
        if "bypass permissions" in line or "⏵⏵" in line:
            continue
        if line.startswith("▐") or line.startswith("▝"):
            continue
        if "ctrl+o to expand" in line:
            continue
        content_lines.append(line)

    return "\n".join(content_lines[-30:])


def write_handoff(worktree: str, issue_id: str, title: str,
                  linear_id: str, branch: str,
                  complexity: str = "", needs_spec: str = "") -> dict:
    """Inspect worktree, determine phase, write handoff. Returns the phase info."""
    phase_info = detect_phase(worktree)

    # Get pane summary for context
    pane_summary = summarize_pane(issue_id)

    # Preserve existing handoff fields if present
    existing = _read_existing_handoff(worktree)
    if not complexity and existing:
        complexity = existing.get("complexity", "")
    if not needs_spec and existing:
        needs_spec = existing.get("needs_spec", "")

    handoff = f"""---
issue_id: {issue_id}
title: {title}
linear_id: {linear_id}
worktree: {worktree}
branch: {branch}
phase: {phase_info['phase']}
complexity: {complexity}
needs_spec: {needs_spec}
"""
    if phase_info.get("spec_path"):
        handoff += f"spec_path: {phase_info['spec_path']}\n"
    if phase_info.get("pr_url"):
        handoff += f"pr_url: {phase_info['pr_url']}\n"

    handoff += f"""---

## Status
{phase_info['summary']}

## Agent activity
{pane_summary[:2000]}
"""

    dispatch_dir = Path(worktree) / ".dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    (dispatch_dir / "handoff.md").write_text(handoff)

    log.info(f"{issue_id}: handoff written — phase={phase_info['phase']}")
    return phase_info


def _read_existing_handoff(worktree: str) -> dict | None:
    """Read existing handoff YAML frontmatter."""
    hf = Path(worktree) / ".dispatch" / "handoff.md"
    if not hf.exists():
        return None
    text = hf.read_text()
    match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not match:
        return None
    import yaml
    try:
        return yaml.safe_load(match.group(1))
    except Exception:
        return None
