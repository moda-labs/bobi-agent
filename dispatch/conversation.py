"""Check Linear issues for human replies."""

import httpx

LINEAR_API = "https://api.linear.app/graphql"

AGENT_PREFIXES = ("🤖",)


async def get_latest_human_reply_after_agent(api_key: str, linear_issue_id: str) -> str | None:
    """Get the most recent human comment that came AFTER the last agent comment.

    Returns None if the latest comment is from the agent (no new human reply).
    This prevents re-spawning on the same reply repeatedly.
    """
    query = """
    query($issueId: String!) {
        issue(id: $issueId) {
            comments(orderBy: createdAt) {
                nodes { body createdAt }
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
    if not comments:
        return None

    # Find the last agent comment and the last human comment
    last_agent_at = None
    last_human_body = None
    last_human_at = None

    for comment in comments:
        body = comment.get("body", "")
        created = comment.get("createdAt", "")
        is_agent = any(body.startswith(p) for p in AGENT_PREFIXES)

        if is_agent:
            last_agent_at = created
        else:
            last_human_body = body
            last_human_at = created

    if not last_human_body:
        return None

    # Human replied after agent's last comment → actionable
    if last_human_at and (not last_agent_at or last_human_at > last_agent_at):
        return last_human_body

    return None
