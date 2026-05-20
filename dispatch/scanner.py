"""Scan Linear for all active issues grouped by state."""

import httpx

from .config import RepoConfig

LINEAR_API = "https://api.linear.app/graphql"


async def scan_linear_all_active(api_key: str, repo_config: RepoConfig) -> dict[str, list[dict]]:
    """Fetch all issues grouped by state name.

    Returns: {"Todo": [issue_data, ...], "In Progress": [...], ...}
    """
    if not repo_config.linear_project:
        return {}

    query = """
    query($team: String!) {
        issues(
            filter: { team: { key: { eq: $team } } }
            first: 50
            orderBy: updatedAt
        ) {
            nodes {
                id identifier title description
                labels { nodes { name } }
                state { name type }
                comments(last: 3) { nodes { body createdAt user { name email } } }
            }
        }
    }
    """

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": {"team": repo_config.linear_project}},
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()

    result: dict[str, list[dict]] = {}
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        state_name = node.get("state", {}).get("name", "Unknown")
        result.setdefault(state_name, []).append(node)
    return result
