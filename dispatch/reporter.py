"""Report results back to Linear and Slack."""

import httpx

from .config import GlobalConfig, RepoConfig
from .state import StateStore, TrackedItem, Status


LINEAR_API = "https://api.linear.app/graphql"


async def update_linear_status(
    global_config: GlobalConfig,
    item_id: str,
    linear_issue_id: str,
    status: Status,
    comment: str | None = None,
) -> None:
    """Update a Linear issue's state and optionally add a comment."""
    if not global_config.linear_api_key or not linear_issue_id:
        return

    # Map our status to Linear workflow states
    state_map = {
        Status.DISPATCHED: "started",
        Status.WORKING: "started",
        Status.AUDITING: "started",
        Status.DONE: "done",
        Status.FAILED: "unstarted",
        Status.STUCK: "unstarted",
    }

    async with httpx.AsyncClient() as client:
        if comment:
            mutation = """
            mutation($issueId: String!, $body: String!) {
                commentCreate(input: { issueId: $issueId, body: $body }) {
                    success
                }
            }
            """
            await client.post(
                LINEAR_API,
                headers={
                    "Authorization": global_config.linear_api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "query": mutation,
                    "variables": {
                        "issueId": linear_issue_id,
                        "body": comment,
                    },
                },
            )


async def post_slack_update(
    global_config: GlobalConfig,
    channel: str,
    text: str,
    thread_ts: str | None = None,
    slack_token: str | None = None,
) -> None:
    """Post a status update to Slack."""
    token = slack_token or global_config.slack_bot_token
    if not token or not channel:
        return

    payload = {
        "channel": channel,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )


async def report_completion(
    global_config: GlobalConfig,
    repo_config: RepoConfig,
    item: TrackedItem,
) -> None:
    """Report that work is done — update Linear, post to Slack."""
    creds = repo_config.get_credentials()
    slack_token = creds.get("slack_bot_token") or global_config.slack_bot_token

    pr_text = f" PR: {item.pr_url}" if item.pr_url else ""
    message = f"Completed **{item.title}** on `{item.repo_path}`.{pr_text}"

    if repo_config.slack_channel:
        await post_slack_update(
            global_config, repo_config.slack_channel, message,
            slack_token=slack_token,
        )


async def report_failure(
    global_config: GlobalConfig,
    repo_config: RepoConfig,
    item: TrackedItem,
) -> None:
    """Report that work failed or got stuck."""
    creds = repo_config.get_credentials()
    slack_token = creds.get("slack_bot_token") or global_config.slack_bot_token

    error_text = f"\nError: {item.error}" if item.error else ""
    message = f"Failed on **{item.title}** (`{item.id}`).{error_text}\nRepo: `{item.repo_path}`"

    if repo_config.slack_channel:
        await post_slack_update(
            global_config, repo_config.slack_channel, message,
            slack_token=slack_token,
        )
