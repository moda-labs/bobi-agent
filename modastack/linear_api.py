"""Minimal Linear helpers for the daemon. Skills handle their own Linear calls."""

import httpx

LINEAR_API = "https://api.linear.app/graphql"


async def get_state_ids(api_key: str, team_key: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": f'{{teams(filter:{{key:{{eq:"{team_key}"}}}}){{nodes{{states{{nodes{{id name}}}}}}}}}}'},
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()

    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    if not teams:
        return {}
    return {s["name"]: s["id"] for s in teams[0]["states"]["nodes"]}


async def move_issue(api_key: str, issue_id: str, state_id: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": f'mutation {{ issueUpdate(id: "{issue_id}", input: {{ stateId: "{state_id}" }}) {{ success }} }}'},
        )
        return resp.json().get("data", {}).get("issueUpdate", {}).get("success", False)


async def add_comment(api_key: str, issue_id: str, body: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "query": 'mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }',
                "variables": {"id": issue_id, "body": body},
            },
        )
        return resp.json().get("data", {}).get("commentCreate", {}).get("success", False)
