"""Scan Linear for actionable work."""

from dataclasses import dataclass
from enum import Enum

import httpx

from .config import GlobalConfig, RepoConfig


class WorkSource(Enum):
    LINEAR = "linear"


class Complexity(Enum):
    TRIVIAL = "trivial"
    MEDIUM = "medium"
    HEAVY = "heavy"


@dataclass
class WorkItem:
    """A unit of work detected from Linear."""

    id: str
    source: WorkSource
    title: str
    body: str
    repo_config: RepoConfig
    complexity: Complexity = Complexity.MEDIUM
    labels: list[str] | None = None
    linear_issue_id: str | None = None


LINEAR_API = "https://api.linear.app/graphql"


async def scan_linear(global_config: GlobalConfig, repo_config: RepoConfig) -> list[WorkItem]:
    """Fetch issues from Linear that match the repo's trigger criteria."""
    # Resolve credentials: per-repo first, fall back to global
    creds = repo_config.get_credentials()
    api_key = creds.get("linear_api_key") or global_config.linear_api_key

    if not api_key or not repo_config.linear_project:
        return []

    query = """
    query($team: String!) {
        issues(
            filter: {
                team: { key: { eq: $team } }
                state: { type: { in: ["unstarted"] } }
            }
            first: 20
            orderBy: updatedAt
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
                    "team": repo_config.linear_project,
                },
            },
        )
        if resp.status_code != 200:
            return []
        data = resp.json()

    items = []
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        issue_labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]

        # Must have at least one trigger label
        if not any(trigger in issue_labels for trigger in repo_config.trigger_labels):
            continue

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
