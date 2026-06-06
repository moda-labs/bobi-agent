"""GitHub Issues adapter — scan issues and bootstrap labels via gh CLI.

Used when task_tracking.system == "github-issues" in .modastack/config.yaml.
No API key needed — uses gh CLI's existing authentication.
"""

import json
import subprocess
from pathlib import Path

from .config import ProjectConfig

WORKFLOW_LABELS = [
    ("status:todo", "Ready to be picked up", "0e8a16"),
    ("status:in-progress", "Engineer actively working", "fbca04"),
    ("status:blocked", "Waiting for human input", "d93f0b"),
    ("status:in-review", "PR created, awaiting review", "0075ca"),
    ("agent", "Modastack-managed issue", "6366f1"),
]


def bootstrap_labels(project_path: Path) -> list[str]:
    """Ensure all workflow labels exist in the GitHub repo."""
    actions = []

    existing = subprocess.run(
        ["gh", "label", "list", "--json", "name", "--limit", "200"],
        capture_output=True, text=True, cwd=project_path,
    )
    if existing.returncode != 0:
        return [f"Failed to list labels: {existing.stderr.strip()}"]

    existing_names = {l["name"] for l in json.loads(existing.stdout)}

    for name, description, color in WORKFLOW_LABELS:
        if name in existing_names:
            continue
        result = subprocess.run(
            ["gh", "label", "create", name, "--description", description, "--color", color],
            capture_output=True, text=True, cwd=project_path,
        )
        if result.returncode == 0:
            actions.append(f"Created label '{name}'")
        else:
            actions.append(f"Failed to create '{name}': {result.stderr.strip()}")

    if not actions:
        actions.append("Labels already configured")

    return actions


WEBHOOK_EVENTS = ["issues", "issue_comment", "pull_request", "pull_request_review", "check_run", "workflow_run"]


def setup_webhook(project_path: Path, public_url: str) -> list[str]:
    """Create a GitHub webhook for the repo pointing at the modastack webhook server.

    Skips if a webhook already exists for this URL.  Requires the gh CLI
    to have admin:repo_hook scope.
    """
    actions = []
    webhook_url = f"{public_url.rstrip('/')}/webhooks/github"

    # Check existing webhooks
    existing = subprocess.run(
        ["gh", "api", "repos/{owner}/{repo}/hooks", "--jq", ".[].config.url"],
        capture_output=True, text=True, cwd=project_path,
    )
    if existing.returncode == 0:
        for line in existing.stdout.strip().splitlines():
            if line.strip() == webhook_url:
                actions.append(f"Webhook already exists: {webhook_url}")
                return actions

    # Build the gh api command
    cmd = [
        "gh", "api", "repos/{owner}/{repo}/hooks",
        "--method", "POST",
        "-f", f"config[url]={webhook_url}",
        "-f", "config[content_type]=json",
        "-F", "active=true",
    ]
    for event in WEBHOOK_EVENTS:
        cmd += ["-f", f"events[]={event}"]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_path)
    if result.returncode == 0:
        actions.append(f"Created webhook: {webhook_url}")
        actions.append(f"  Events: {', '.join(WEBHOOK_EVENTS)}")
    else:
        stderr = result.stderr.strip()
        if "Resource not accessible" in stderr or "Not Found" in stderr:
            actions.append(
                "Webhook setup skipped — gh CLI lacks admin:repo_hook scope. "
                "Run: gh auth refresh -h github.com -s admin:repo_hook"
            )
        else:
            actions.append(f"Failed to create webhook: {stderr}")

    return actions


def scan_github_issues(project_config: ProjectConfig) -> dict[str, list[dict]]:
    """Fetch open issues assigned to the bot account, grouped by workflow state label.

    Only returns issues assigned to the bot (from config github.default_account).
    Labels are not used for triggering — only assignment matters.

    Returns: {"Todo": [issue_data, ...], "In Progress": [...], ...}
    """
    result = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--assignee", "@me",
         "--json", "number,title,body,labels,comments,assignees,url", "--limit", "50"],
        capture_output=True, text=True, cwd=project_config.path,
    )
    if result.returncode != 0:
        return {}

    issues = json.loads(result.stdout)
    skip_labels = {"blocked", "human-only"}
    label_to_state = {
        "status:todo": "Todo",
        "status:in-progress": "In Progress",
        "status:blocked": "Blocked",
        "status:in-review": "In Review",
    }

    grouped: dict[str, list[dict]] = {}
    for issue in issues:
        label_names = [l["name"] for l in issue.get("labels", [])]

        if skip_labels & set(label_names):
            continue

        state = "Todo"
        for label_name, state_name in label_to_state.items():
            if label_name in label_names:
                state = state_name
                break

        repo_short = (project_config.github_repo.split("/")[-1] if project_config.github_repo
                      else project_config.path.name).upper()[:6]
        project = repo_short
        identifier = f"{project}-{issue['number']}"

        normalized = {
            "id": str(issue["number"]),
            "identifier": identifier,
            "title": issue["title"],
            "description": issue.get("body") or "",
            "labels": {"nodes": [{"name": n} for n in label_names]},
            "state": {"name": state},
            "comments": {
                "nodes": [
                    {"body": c.get("body", ""), "user": {"name": c.get("author", {}).get("login", "")}}
                    for c in (issue.get("comments") or [])[-3:]
                ]
            },
        }
        grouped.setdefault(state, []).append(normalized)

    return grouped
