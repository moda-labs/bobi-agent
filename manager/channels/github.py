"""GitHub PR review comments channel."""

import json
import shutil
import subprocess
from pathlib import Path

from dispatch.config import GlobalConfig


async def gather(config: dict) -> list[dict]:
    """Fetch PR state and review comments for agent branches."""
    global_config = GlobalConfig.load()
    gh = shutil.which("gh") or "gh"
    items = []
    seen = set()

    for repo_path in global_config.repos:
        # Check worktrees (active sessions)
        wt_dir = repo_path / "worktrees"
        if wt_dir.exists():
            for child in wt_dir.iterdir():
                if not child.is_dir():
                    continue
                issue_id = child.name.upper()
                pr = _fetch_pr(gh, str(child))
                if pr:
                    items.append(pr | {"issue_id": issue_id})
                    seen.add(issue_id)

        # Also check for agent branches without worktrees (session killed but PR still open)
        result = subprocess.run(
            [gh, "pr", "list", "--json", "headRefName,url,state", "--state", "all", "--limit", "20"],
            cwd=str(repo_path), capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        try:
            prs = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            continue

        for pr in prs:
            branch = pr.get("headRefName", "")
            if not branch.startswith("agent/"):
                continue
            issue_id = branch.replace("agent/", "").upper()
            if issue_id in seen:
                continue

            # Fetch full details for this PR
            detail = _fetch_pr(gh, str(repo_path), branch=branch)
            if detail:
                items.append(detail | {"issue_id": issue_id})
                seen.add(issue_id)

    return items


def _fetch_pr(gh: str, cwd: str, branch: str | None = None) -> dict | None:
    """Fetch PR data. Returns dict or None."""
    cmd = [gh, "pr", "view", "--json", "url,state,comments,reviews"]
    if branch:
        cmd.append(branch)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        pr_data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    comments = []
    for c in pr_data.get("comments", [])[-3:]:
        comments.append({
            "author": c.get("author", {}).get("login", "unknown"),
            "body": c.get("body", "")[:300],
        })
    for r in pr_data.get("reviews", [])[-3:]:
        if r.get("body"):
            comments.append({
                "author": r.get("author", {}).get("login", "unknown"),
                "body": r.get("body", "")[:300],
            })

    return {
        "channel": "github",
        "pr_url": pr_data.get("url", ""),
        "pr_state": pr_data.get("state", ""),
        "comments": comments,
    }


def hash_key(items: list[dict]) -> str:
    """Change detection — PR state + latest comment."""
    parts = []
    for i in items:
        latest = i.get("comments", [{}])[-1].get("body", "")[:50] if i.get("comments") else ""
        parts.append(f"{i['issue_id']}:{i['pr_state']}:{latest}")
    return "|".join(sorted(parts))


def format_context(items: list[dict]) -> str:
    """Format for manager prompt."""
    if not items:
        return ""
    lines = ["\n## GitHub PRs"]
    for i in items:
        lines.append(f"- **{i['issue_id']}**: {i['pr_url']} ({i['pr_state']})")
        for c in i.get("comments", []):
            lines.append(f"  🔍 [{c['author']}]: {c['body'][:150]}")
    return "\n".join(lines)
