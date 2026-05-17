"""Parse SPEC.md for ticket breakdown and create sub-tickets in Linear."""

import re
import logging

import httpx
import yaml

from .config import RepoConfig

log = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"


def parse_split_from_spec(spec_content: str) -> dict | None:
    """Parse the YAML ticket breakdown from SPEC.md.

    Returns {"split": True, "tickets": [...]} or None if no split.
    """
    # Find YAML blocks in the spec
    yaml_blocks = re.findall(r'```yaml\s*\n(.*?)```', spec_content, re.DOTALL)

    for block in yaml_blocks:
        try:
            data = yaml.safe_load(block)
            if isinstance(data, dict) and data.get("split") is True:
                return data
        except yaml.YAMLError:
            continue

    # Also check for inline split: true/false
    if "split: false" in spec_content:
        return {"split": False}

    return None


async def create_sub_tickets(
    api_key: str,
    parent_issue_id: str,
    team_key: str,
    tickets: list[dict],
    trigger_labels: list[str],
) -> list[dict]:
    """Create sub-tickets in Linear linked to the parent issue.

    Returns list of created ticket info: [{id, identifier, title}]
    """
    # Get team ID
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": '{teams(filter:{key:{eq:"' + team_key + '"}}){nodes{id}}}'},
        )
        teams = resp.json().get("data", {}).get("teams", {}).get("nodes", [])
        if not teams:
            return []
        team_id = teams[0]["id"]

    # Get label IDs for trigger labels (so sub-tickets get auto-dispatched)
    label_ids = []
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": '{issueLabels(filter:{name:{in:' + str(trigger_labels).replace("'", '"') + '}}){nodes{id name}}}'},
        )
        labels = resp.json().get("data", {}).get("issueLabels", {}).get("nodes", [])
        label_ids = [l["id"] for l in labels]

    created = []
    for ticket in tickets:
        title = ticket.get("title", "")
        description = ticket.get("description", "")
        depends_on = ticket.get("depends_on", [])

        dep_text = ""
        if depends_on:
            dep_text = f"\n\nDepends on: {', '.join(depends_on)}"

        mutation = """
        mutation($teamId: String!, $title: String!, $description: String!, $parentId: String!, $labelIds: [String!]) {
            issueCreate(input: {
                teamId: $teamId
                title: $title
                description: $description
                parentId: $parentId
                labelIds: $labelIds
            }) {
                success
                issue { id identifier title }
            }
        }
        """

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LINEAR_API,
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={
                    "query": mutation,
                    "variables": {
                        "teamId": team_id,
                        "title": title,
                        "description": f"{description}{dep_text}",
                        "parentId": parent_issue_id,
                        "labelIds": label_ids,
                    },
                },
            )
            data = resp.json()
            issue = data.get("data", {}).get("issueCreate", {}).get("issue")
            if issue:
                created.append(issue)
                log.info(f"Created sub-ticket {issue['identifier']}: {issue['title']}")

    return created
