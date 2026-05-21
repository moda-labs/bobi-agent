"""Linear ticketing channel."""

import asyncio
from modastack.config import GlobalConfig, RepoConfig
from modastack.scanner import scan_linear_all_active


async def gather(config: dict) -> list[dict]:
    """Fetch all issues from Linear across registered repos."""
    global_config = GlobalConfig.load()
    items = []

    for repo_path in global_config.repos:
        if not repo_path.exists():
            continue
        try:
            repo_config = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            continue

        creds = repo_config.get_credentials()
        api_key = creds.get("linear_api_key") or global_config.linear_api_key
        if not api_key:
            continue

        issues_by_state = await scan_linear_all_active(api_key, repo_config)
        for state_name, issues in issues_by_state.items():
            # Load repo-specific context from .modastack.yaml
            import yaml
            dispatch_yaml = repo_config.path / ".modastack.yaml"
            repo_context = ""
            if dispatch_yaml.exists():
                raw = yaml.safe_load(dispatch_yaml.read_text()) or {}
                ctx = raw.get("context", {})
                if ctx:
                    notes = ctx.get("notes", "")
                    repo_context = notes.strip() if isinstance(notes, str) else ""

            for issue in issues:
                labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                comments = []
                for c in issue.get("comments", {}).get("nodes", []):
                    comments.append({
                        "author": c.get("user", {}).get("name", "unknown"),
                        "body": c.get("body", "")[:300],
                        "created": c.get("createdAt", ""),
                    })
                items.append({
                    "channel": "linear",
                    "id": issue["identifier"],
                    "linear_id": issue["id"],
                    "title": issue["title"],
                    "description": (issue.get("description") or "")[:500],
                    "state": state_name,
                    "labels": labels,
                    "repo": str(repo_config.path),
                    "project": repo_config.linear_project,
                    "recent_comments": comments,
                    "repo_context": repo_context,
                })
    return items


def hash_key(items: list[dict]) -> str:
    """Change detection key — state + latest comment per issue."""
    parts = []
    for i in items:
        latest = i.get("recent_comments", [{}])[-1].get("body", "")[:50] if i.get("recent_comments") else ""
        parts.append(f"{i['id']}:{i['state']}:{latest}")
    return "|".join(sorted(parts))


def format_context(items: list[dict]) -> str:
    """Format for manager prompt."""
    lines = ["## Linear Issues"]
    by_state = {}
    for i in items:
        by_state.setdefault(i["state"], []).append(i)

    for state in ["Todo", "In Progress", "Blocked", "In Review"]:
        issues = by_state.get(state, [])
        if not issues:
            continue
        lines.append(f"\n### {state}")
        for i in issues:
            labels = ", ".join(i["labels"]) if i["labels"] else "no labels"
            lines.append(f"- **{i['id']}** (linear_id: {i['linear_id']}): {i['title']} [{i['project']}] ({labels}) repo: {i['repo']}")
            if i["description"]:
                lines.append(f"  {i['description'][:200]}")
            for c in i.get("recent_comments", []):
                lines.append(f"  💬 [{c['author']}]: {c['body'][:150]}")
            if i.get("repo_context"):
                lines.append(f"  ⚙️ Repo notes: {i['repo_context'][:200]}")
    return "\n".join(lines)
