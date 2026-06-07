"""GitHub checks for the engineering_org agent pack.

Includes the base SDLC checks (PR conflicts, stale PRs) plus a
project_health composite check that detects CI failures on main,
unreviewed PRs, broken builds on PR branches, and duplicate issues.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from modastack.monitors.schema import Condition

log = logging.getLogger(__name__)

GH_TIMEOUT = 30


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


def _gh_issue_list(repo: Path, fields: list[str]) -> list[dict]:
    """`gh issue list` for open issues in a repo, returning parsed JSON (or [])."""
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--limit", "100",
             "--json", ",".join(fields)],
            cwd=str(repo), capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"gh issue list failed in {repo}: {e}")
        return []
    if out.returncode != 0:
        log.warning(f"gh issue list failed in {repo}: {out.stderr.strip()}")
        return []
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _gh_run_list_main(repo: Path) -> list[dict]:
    """Get the latest CI run on main branch."""
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--branch", "main", "--limit", "1",
             "--json", "conclusion"],
            cwd=str(repo), capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"gh run list failed in {repo}: {e}")
        return []
    if out.returncode != 0:
        log.warning(f"gh run list failed in {repo}: {out.stderr.strip()}")
        return []
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []


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


def _title_words(title: str) -> set[str]:
    """Extract meaningful words from an issue title for overlap comparison."""
    stop_words = {"a", "an", "the", "is", "in", "on", "at", "to", "for",
                  "of", "and", "or", "not", "it", "be", "as", "with", "by",
                  "this", "that", "from", "but", "are", "was", "were", "has",
                  "have", "had", "do", "does", "did", "will", "would", "can",
                  "could", "should", "may", "might"}
    words = set()
    for word in title.lower().split():
        cleaned = "".join(c for c in word if c.isalnum())
        if cleaned and len(cleaned) > 2 and cleaned not in stop_words:
            words.add(cleaned)
    return words


def pr_conflicts(monitor, projects: list[Path]) -> list[Condition]:
    """Open PRs whose mergeable status is CONFLICTING."""
    conditions: list[Condition] = []
    for project in projects:
        slug = _repo_slug(project)
        for pr in _gh_pr_list(project, ["number", "title", "url", "mergeable", "headRefName"]):
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


def stale_prs(monitor, projects: list[Path]) -> list[Condition]:
    """Open, non-draft PRs with no activity within the threshold (default 48h)."""
    threshold_hours = int(monitor.extra.get("threshold_hours", 48))
    now = datetime.now(timezone.utc)
    conditions: list[Condition] = []
    for project in projects:
        slug = _repo_slug(project)
        for pr in _gh_pr_list(project, ["number", "title", "url", "updatedAt", "isDraft"]):
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


def project_health(monitor, projects: list[Path]) -> list[Condition]:
    """Composite health check: CI on main, unreviewed PRs, broken builds, duplicate issues."""
    conditions: list[Condition] = []
    now = datetime.now(timezone.utc)

    for project in projects:
        slug = _repo_slug(project)

        # 1. CI failing on main
        runs = _gh_run_list_main(project)
        if runs and runs[0].get("conclusion") == "failure":
            conditions.append(Condition(
                key=f"ci:{slug}:main",
                data={
                    "repo": slug,
                    "check": "ci_main",
                    "detail": "Latest CI run on main is failing",
                },
            ))

        # 2. Unreviewed PRs (>24h) and 3. Broken builds on PR branches
        prs = _gh_pr_list(project, [
            "number", "title", "url", "createdAt", "isDraft",
            "reviews", "statusCheckRollup",
        ])
        for pr in prs:
            if pr.get("isDraft"):
                continue
            num = pr.get("number")

            # Unreviewed PRs older than 24h
            created = _parse_iso(pr.get("createdAt", ""))
            reviews = pr.get("reviews") or []
            if created and not reviews:
                age_hours = (now - created).total_seconds() / 3600
                if age_hours >= 24:
                    conditions.append(Condition(
                        key=f"unreviewed:{slug}#{num}",
                        data={
                            "repo": slug,
                            "check": "unreviewed_pr",
                            "pr_number": num,
                            "title": pr.get("title", ""),
                            "url": pr.get("url", ""),
                            "age_hours": round(age_hours, 1),
                        },
                    ))

            # Broken builds on PR branches
            rollup = pr.get("statusCheckRollup") or []
            failures = [
                c for c in rollup
                if c.get("conclusion") == "FAILURE"
            ]
            if failures:
                conditions.append(Condition(
                    key=f"ci-fail:{slug}#{num}",
                    data={
                        "repo": slug,
                        "check": "ci_pr_failure",
                        "pr_number": num,
                        "title": pr.get("title", ""),
                        "url": pr.get("url", ""),
                        "failed_checks": len(failures),
                    },
                ))

        # 4. Duplicate issues (simple word-overlap heuristic)
        issues = _gh_issue_list(project, ["number", "title", "url"])
        # Build word sets for each issue
        issue_data = []
        for issue in issues:
            title = issue.get("title", "")
            words = _title_words(title)
            if words:
                issue_data.append((issue, words))

        # Compare all pairs for significant overlap
        for i, (issue_a, words_a) in enumerate(issue_data):
            for j in range(i + 1, len(issue_data)):
                issue_b, words_b = issue_data[j]
                overlap = words_a & words_b
                # Require at least 3 overlapping words and >50% of the smaller set
                smaller = min(len(words_a), len(words_b))
                if len(overlap) >= 3 and len(overlap) / smaller > 0.5:
                    num_a = issue_a.get("number")
                    num_b = issue_b.get("number")
                    # Canonical ordering so the key is stable
                    lo, hi = sorted([num_a, num_b])
                    conditions.append(Condition(
                        key=f"dup:{slug}#{lo}+{hi}",
                        data={
                            "repo": slug,
                            "check": "duplicate_issues",
                            "issue_a": lo,
                            "issue_b": hi,
                            "title_a": issue_a.get("title", "") if num_a == lo else issue_b.get("title", ""),
                            "title_b": issue_b.get("title", "") if num_a == lo else issue_a.get("title", ""),
                            "overlap_words": sorted(overlap),
                        },
                    ))

    return conditions


# Native check runners, keyed by the monitor's `check` field.
CHECKS = {
    "pr_conflicts": pr_conflicts,
    "stale_prs": stale_prs,
    "project_health": project_health,
}
