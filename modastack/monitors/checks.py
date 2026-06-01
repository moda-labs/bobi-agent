"""Native check runners for built-in monitors.

A check runner takes a Monitor and the list of repos it applies to and
returns the set of *conditions* currently true. Each condition carries a
stable `key` (used by the scheduler to deduplicate) and an event `data`
payload. The scheduler diffs conditions against prior state and only
injects events for newly-appeared ones.

Monitors that name no `check` are interpreted by the manager instead
(see scheduler._manager_interpreted).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

GH_TIMEOUT = 30


@dataclass
class Condition:
    """One detected condition: a dedup key plus the event payload."""

    key: str
    data: dict = field(default_factory=dict)


_slug_cache: dict[str, str] = {}


def _repo_slug(repo: Path) -> str:
    """Resolve a repo's org/name slug, falling back to the directory name."""
    cached = _slug_cache.get(str(repo))
    if cached:
        return cached
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=str(repo), capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
        slug = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        slug = ""
    slug = slug or repo.name
    _slug_cache[str(repo)] = slug
    return slug


def _gh_pr_list(repo: Path, fields: list[str]) -> list[dict]:
    """`gh pr list` for open PRs in a repo, returning parsed JSON (or [])."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "50",
             "--json", ",".join(fields)],
            cwd=str(repo), capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"gh pr list failed in {repo}: {e}")
        return []
    if out.returncode != 0:
        log.warning(f"gh pr list failed in {repo}: {out.stderr.strip()}")
        return []
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []


def pr_conflicts(monitor, repos: list[Path]) -> list[Condition]:
    """Open PRs whose mergeable status is CONFLICTING."""
    conditions: list[Condition] = []
    for repo in repos:
        slug = _repo_slug(repo)
        for pr in _gh_pr_list(repo, ["number", "title", "url", "mergeable", "headRefName"]):
            if pr.get("mergeable") != "CONFLICTING":
                continue
            num = pr.get("number")
            conditions.append(Condition(
                key=f"{slug}#{num}",
                data={
                    "repo": slug,
                    "pr_number": num,
                    "title": pr.get("title", ""),
                    "branch": pr.get("headRefName", ""),
                    "url": pr.get("url", ""),
                },
            ))
    return conditions


def stale_prs(monitor, repos: list[Path]) -> list[Condition]:
    """Open, non-draft PRs with no activity within the threshold (default 48h)."""
    threshold_hours = int(monitor.extra.get("threshold_hours", 48))
    now = datetime.now(timezone.utc)
    conditions: list[Condition] = []
    for repo in repos:
        slug = _repo_slug(repo)
        for pr in _gh_pr_list(repo, ["number", "title", "url", "updatedAt", "isDraft"]):
            if pr.get("isDraft"):
                continue
            updated = _parse_iso(pr.get("updatedAt", ""))
            if updated is None:
                continue
            age_hours = (now - updated).total_seconds() / 3600
            if age_hours < threshold_hours:
                continue
            num = pr.get("number")
            conditions.append(Condition(
                key=f"{slug}#{num}",
                data={
                    "repo": slug,
                    "pr_number": num,
                    "title": pr.get("title", ""),
                    "url": pr.get("url", ""),
                    "idle_hours": round(age_hours, 1),
                },
            ))
    return conditions


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Native check runners, keyed by the monitor's `check` field.
CHECKS = {
    "pr_conflicts": pr_conflicts,
    "stale_prs": stale_prs,
}
