"""Linear-based conversation: post questions, poll for replies, resume sessions."""

import httpx

from .config import GlobalConfig
from .state import StateStore, Status

LINEAR_API = "https://api.linear.app/graphql"

# Prefix on comments posted by the agent so we can identify them
AGENT_COMMENT_PREFIX = "🤖 **Agent question:**"
AGENT_STATUS_PREFIX = "🤖 **Agent update:**"


async def post_question(
    api_key: str,
    linear_issue_id: str,
    question: str,
) -> str | None:
    """Post a question as a comment on the Linear issue. Returns comment ID."""
    body = f"{AGENT_COMMENT_PREFIX}\n\n{question}"

    mutation = """
    mutation($issueId: String!, $body: String!) {
        commentCreate(input: { issueId: $issueId, body: $body }) {
            success
            comment { id }
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
                "query": mutation,
                "variables": {"issueId": linear_issue_id, "body": body},
            },
        )
        data = resp.json()

    comment = data.get("data", {}).get("commentCreate", {}).get("comment", {})
    return comment.get("id")


async def post_status(
    api_key: str,
    linear_issue_id: str,
    status_message: str,
) -> None:
    """Post a status update as a comment on the Linear issue."""
    body = f"{AGENT_STATUS_PREFIX}\n\n{status_message}"

    mutation = """
    mutation($issueId: String!, $body: String!) {
        commentCreate(input: { issueId: $issueId, body: $body }) {
            success
        }
    }
    """

    async with httpx.AsyncClient() as client:
        await client.post(
            LINEAR_API,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            json={
                "query": mutation,
                "variables": {"issueId": linear_issue_id, "body": body},
            },
        )


async def check_for_reply(
    api_key: str,
    linear_issue_id: str,
    after_comment_id: str,
) -> str | None:
    """Check if the user replied after our question comment.

    Returns the reply text if found, None otherwise.
    """
    query = """
    query($issueId: String!) {
        issue(id: $issueId) {
            comments(orderBy: createdAt) {
                nodes {
                    id
                    body
                    user { isMe }
                    createdAt
                }
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
                "variables": {"issueId": linear_issue_id},
            },
        )
        data = resp.json()

    comments = data.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])

    # Find our question comment, then look for a non-agent reply after it
    found_our_comment = False
    for comment in comments:
        if comment["id"] == after_comment_id:
            found_our_comment = True
            continue

        if found_our_comment:
            body = comment.get("body", "")
            # Skip our own comments (agent prefix)
            if body.startswith(AGENT_COMMENT_PREFIX) or body.startswith(AGENT_STATUS_PREFIX):
                continue
            # This is a human reply
            return body

    return None


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

    # Walk backwards to find the most recent human comment
    for comment in reversed(comments):
        body = comment.get("body", "")
        if body.startswith(AGENT_COMMENT_PREFIX) or body.startswith(AGENT_STATUS_PREFIX):
            continue
        if "🤖" in body:
            continue
        return body

    return None


async def poll_blocked_sessions(
    global_config: GlobalConfig,
    state: StateStore,
) -> list[dict]:
    """Check all blocked sessions for user replies. Returns unblocked items."""
    unblocked = []

    for item in state.get_in_flight():
        if item.status != Status.BLOCKED:
            continue

        if not item.linear_issue_id:
            continue

        # Resolve credentials for this repo
        from .config import RepoConfig
        from pathlib import Path
        try:
            repo_config = RepoConfig.from_file(Path(item.repo_path))
            creds = repo_config.get_credentials()
            api_key = creds.get("linear_api_key") or global_config.linear_api_key
        except FileNotFoundError:
            api_key = global_config.linear_api_key

        if not api_key:
            continue

        if item.pending_question_id:
            # Has a specific question — look for reply after it
            reply = await check_for_reply(
                api_key, item.linear_issue_id, item.pending_question_id
            )
        else:
            # Spec review or general block — look for any recent human comment
            reply = await get_latest_human_comment(api_key, item.linear_issue_id)

        if reply:
            unblocked.append({
                "id": item.id,
                "reply": reply,
                "linear_issue_id": item.linear_issue_id,
            })
            state.update_status(
                item.id, Status.WORKING,
                pending_question_id=None,
                last_reply=reply,
            )

    return unblocked
