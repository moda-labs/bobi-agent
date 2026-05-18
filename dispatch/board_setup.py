"""Bootstrap Linear workflow states for agentd lifecycle."""

import httpx

LINEAR_API = "https://api.linear.app/graphql"

REQUIRED_STATES = [
    ("In Progress", "started", "#f59e0b", 0),
    ("Blocked", "started", "#eb5757", 1),
    ("In Review", "started", "#10b981", 2),
]


def bootstrap_board(api_key: str, team_key: str) -> list[str]:
    """Ensure all required workflow states exist for the team.

    Creates missing states, repositions existing ones.
    Returns list of actions taken.
    """
    actions = []

    # Get team ID and existing states
    resp = httpx.post(
        LINEAR_API,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"query": f'{{teams(filter:{{key:{{eq:"{team_key}"}}}}){{nodes{{id states{{nodes{{id name type position}}}}}}}}}}'},
    )
    data = resp.json()
    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    if not teams:
        return [f"Team '{team_key}' not found"]

    team_id = teams[0]["id"]
    existing = {s["name"]: s for s in teams[0]["states"]["nodes"]}

    # Create missing states
    for name, type_, color, position in REQUIRED_STATES:
        if name in existing:
            # Reposition if needed
            current = existing[name]
            if current["position"] != position:
                httpx.post(
                    LINEAR_API,
                    headers={"Authorization": api_key, "Content-Type": "application/json"},
                    json={"query": f'mutation {{ workflowStateUpdate(id: "{current["id"]}", input: {{ position: {position} }}) {{ success }} }}'},
                )
                actions.append(f"Repositioned '{name}' to {position}")
        else:
            resp = httpx.post(
                LINEAR_API,
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={
                    "query": """mutation($teamId: String!, $name: String!, $type: String!, $color: String!, $position: Float!) {
                        workflowStateCreate(input: { teamId: $teamId, name: $name, type: $type, color: $color, position: $position }) {
                            success
                        }
                    }""",
                    "variables": {"teamId": team_id, "name": name, "type": type_, "color": color, "position": position},
                },
            )
            actions.append(f"Created '{name}'")

    # Ensure 'agent' label exists
    resp = httpx.post(
        LINEAR_API,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"query": f'{{issueLabels(filter:{{name:{{eq:"agent"}}}}){{nodes{{id}}}}}}'},
    )
    labels = resp.json().get("data", {}).get("issueLabels", {}).get("nodes", [])
    if not labels:
        httpx.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "query": """mutation($teamId: String!, $name: String!, $color: String!) {
                    issueLabelCreate(input: { teamId: $teamId, name: $name, color: $color }) { success }
                }""",
                "variables": {"teamId": team_id, "name": "agent", "color": "#6366f1"},
            },
        )
        actions.append("Created 'agent' label")

    if not actions:
        actions.append("Board already configured")

    return actions
