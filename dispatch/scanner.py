"""Scan Linear and Slack for actionable work."""

from dataclasses import dataclass
from enum import Enum

import httpx

from .config import GlobalConfig, RepoConfig


class WorkSource(Enum):
    LINEAR = "linear"
    SLACK = "slack"


class Complexity(Enum):
    TRIVIAL = "trivial"
    MEDIUM = "medium"
    HEAVY = "heavy"


@dataclass
class WorkItem:
    """A unit of work detected from Linear or Slack."""

    id: str
    source: WorkSource
    title: str
    body: str
    repo_config: RepoConfig
    complexity: Complexity = Complexity.MEDIUM
    labels: list[str] | None = None
    linear_issue_id: str | None = None
    slack_thread_ts: str | None = None
    slack_channel: str | None = None


LINEAR_API = "https://api.linear.app/graphql"


async def scan_linear(global_config: GlobalConfig, repo_config: RepoConfig) -> list[WorkItem]:
    """Fetch issues from Linear that match the repo's trigger criteria."""
    # Resolve credentials: per-repo first, fall back to global
    creds = repo_config.get_credentials()
    api_key = creds.get("linear_api_key") or global_config.linear_api_key

    if not api_key or not repo_config.linear_project:
        return []

    query = """
    query($project: String!, $labels: [String!]!) {
        issues(
            filter: {
                project: { key: { eq: $project } }
                labels: { name: { in: $labels } }
                state: { type: { in: ["triage", "unstarted"] } }
            }
            first: 20
            orderBy: priority
        ) {
            nodes {
                id
                identifier
                title
                description
                labels { nodes { name } }
                priority
                estimate
            }
        }
    }
    """

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": {
                    "project": repo_config.linear_project,
                    "labels": repo_config.trigger_labels,
                },
            },
        )
        if resp.status_code != 200:
            return []
        data = resp.json()

    items = []
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        issue_labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]

        if any(skip in issue_labels for skip in repo_config.skip_labels):
            continue

        complexity = classify_complexity(node, issue_labels, repo_config)

        items.append(WorkItem(
            id=node["identifier"],
            source=WorkSource.LINEAR,
            title=node["title"],
            body=node.get("description") or "",
            repo_config=repo_config,
            complexity=complexity,
            labels=issue_labels,
            linear_issue_id=node["id"],
        ))

    return items


def classify_complexity(
    issue: dict, labels: list[str], config: RepoConfig
) -> Complexity:
    """Determine issue complexity from labels, estimates, and config rules."""
    rules = config.complexity_rules

    if "trivial" in rules:
        rule = rules["trivial"]
        if _matches_rule(rule, labels, issue):
            return Complexity.TRIVIAL

    if "heavy" in rules:
        rule = rules["heavy"]
        if _matches_rule(rule, labels, issue):
            return Complexity.HEAVY

    return Complexity.MEDIUM


def _matches_rule(rule: str, labels: list[str], issue: dict) -> bool:
    """Simple rule matching: 'label:X OR label:Y' and 'estimate>N'."""
    parts = [p.strip() for p in rule.split(" OR ")]
    for part in parts:
        if part.startswith("label:"):
            if part[6:] in labels:
                return True
        elif part.startswith("estimate>"):
            threshold = int(part[9:])
            if (issue.get("estimate") or 0) > threshold:
                return True
    return False


async def scan_slack(global_config: GlobalConfig, slack_token: str | None = None) -> list[WorkItem]:
    """Scan Slack for actionable messages (DMs/mentions with work intent)."""
    token = slack_token or global_config.slack_bot_token
    if not token:
        return []

    # Fetch unread DMs and mentions from the bot's conversations
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"types": "im", "limit": 20},
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        return []

    items = []
    async with httpx.AsyncClient() as client:
        for channel in data.get("channels", []):
            if not channel.get("is_im"):
                continue

            hist_resp = await client.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "channel": channel["id"],
                    "limit": 5,
                    "unreads": "true",
                },
            )
            hist_data = hist_resp.json()
            if not hist_data.get("ok"):
                continue

            for msg in hist_data.get("messages", []):
                text = msg.get("text", "")
                if not _looks_actionable(text):
                    continue

                items.append(WorkItem(
                    id=f"slack-{msg['ts']}",
                    source=WorkSource.SLACK,
                    title=text[:80],
                    body=text,
                    repo_config=None,  # needs repo resolution
                    slack_thread_ts=msg["ts"],
                    slack_channel=channel["id"],
                ))

    return items


def _looks_actionable(text: str) -> bool:
    """Quick heuristic: does this message look like a work request?"""
    action_words = ["fix", "build", "add", "create", "update", "deploy", "bug", "broken", "implement"]
    text_lower = text.lower()
    return any(word in text_lower for word in action_words)
