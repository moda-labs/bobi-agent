"""Report results back to Linear and Slack."""

import httpx

from .config import GlobalConfig, RepoConfig
from .state import StateStore, TrackedItem, Status


LINEAR_API = "https://api.linear.app/graphql"


async def get_team_states(api_key: str, team_key: str) -> dict[str, str]:
    """Fetch workflow state IDs for a team. Returns {state_type: state_id}."""
    query = """
    query($team: String!) {
        teams(filter: { key: { eq: $team } }) {
            nodes {
                states { nodes { id name type } }
            }
        }
    }
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": {"team": team_key}},
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()

    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    if not teams:
        return {}

    states = {}
    for state in teams[0].get("states", {}).get("nodes", []):
        states[state["name"].lower()] = state["id"]
        states[state["type"]] = state["id"]
    return states


async def move_issue(api_key: str, linear_issue_id: str, state_id: str) -> None:
    """Move a Linear issue to a new workflow state."""
    mutation = """
    mutation($issueId: String!, $stateId: String!) {
        issueUpdate(id: $issueId, input: { stateId: $stateId }) {
            success
        }
    }
    """
    async with httpx.AsyncClient() as client:
        await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "query": mutation,
                "variables": {"issueId": linear_issue_id, "stateId": state_id},
            },
        )


async def move_to_in_progress(api_key: str, linear_issue_id: str, team_key: str) -> None:
    """Move issue to In Progress when work starts."""
    states = await get_team_states(api_key, team_key)
    state_id = states.get("in progress") or states.get("started")
    if state_id:
        await move_issue(api_key, linear_issue_id, state_id)


async def move_to_done(api_key: str, linear_issue_id: str, team_key: str) -> None:
    """Move issue to Done when PR is merged."""
    states = await get_team_states(api_key, team_key)
    state_id = states.get("done") or states.get("completed")
    if state_id:
        await move_issue(api_key, linear_issue_id, state_id)


async def move_to_in_review(api_key: str, linear_issue_id: str, team_key: str) -> None:
    """Move issue to In Review (or Done if no review state) when PR is created."""
    states = await get_team_states(api_key, team_key)
    # Try "in review" first, fall back to "done"
    state_id = states.get("in review") or states.get("completed")
    if state_id:
        await move_issue(api_key, linear_issue_id, state_id)


async def add_comment(api_key: str, linear_issue_id: str, body: str) -> None:
    """Add a comment to a Linear issue."""
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
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "query": mutation,
                "variables": {"issueId": linear_issue_id, "body": body},
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
