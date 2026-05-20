"""GitHub PR review comments channel."""

import json
import shutil
import subprocess
from pathlib import Path

from dispatch.config import GlobalConfig


async def gather(config: dict) -> list[dict]:
    """Fetch PR review comments for worktrees with open PRs."""
    global_config = GlobalConfig.load()
    gh = shutil.which("gh") or "gh"
    items = []

    for repo_path in global_config.repos:
        wt_dir = repo_path / "worktrees"
        if not wt_dir.exists():
            continue
        for child in wt_dir.iterdir():
            if not child.is_dir():
                continue
            issue_id = child.name.upper()
            result = subprocess.run(
                [gh, "pr", "view", "--json", "url,state,comments,reviews"],
                cwd=str(child), capture_output=True, text=True,
            )
            if result.returncode != 0:
                continue
            try:
                pr_data = json.loads(result.stdout)
            except (json.JSONDecodeError, ValueError):
                continue

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

            items.append({
                "channel": "github",
                "issue_id": issue_id,
                "pr_url": pr_data.get("url", ""),
                "pr_state": pr_data.get("state", ""),
                "comments": comments,
            })
    return items


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
