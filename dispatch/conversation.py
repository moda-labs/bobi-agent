"""Check Linear issues for human replies."""

import httpx

from .config import GlobalConfig

LINEAR_API = "https://api.linear.app/graphql"

AGENT_PREFIXES = ("🤖",)


async def get_latest_human_comment(api_key: str, linear_issue_id: str) -> str | None:
    """Get the most recent non-agent comment on an issue."""
    query = """
    query($issueId: String!) {
        issue(id: $issueId) {
            comments(orderBy: createdAt) {
                nodes { id body createdAt }
            }
        }
    }
    """

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": {"issueId": linear_issue_id}},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

    comments = data.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])

    for comment in reversed(comments):
        body = comment.get("body", "")
        if any(body.startswith(p) for p in AGENT_PREFIXES):
            continue
        return body

    return None
