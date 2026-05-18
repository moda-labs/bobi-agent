"""All Linear state transitions happen here. One place, deterministic."""

import shutil
import subprocess
from pathlib import Path

import httpx

LINEAR_API = "https://api.linear.app/graphql"


async def get_state_ids(api_key: str, team_key: str) -> dict[str, str]:
    """Fetch state name → ID mapping for a team. Cached per cycle."""
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
    """Move a Linear issue to a new state."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": f'mutation {{ issueUpdate(id: "{issue_id}", input: {{ stateId: "{state_id}" }}) {{ success }} }}'},
        )
        data = resp.json()
        return data.get("data", {}).get("issueUpdate", {}).get("success", False)


async def add_comment(api_key: str, issue_id: str, body: str) -> bool:
    """Post a comment on a Linear issue."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LINEAR_API,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "query": 'mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }',
                "variables": {"id": issue_id, "body": body},
            },
        )
        data = resp.json()
        return data.get("data", {}).get("commentCreate", {}).get("success", False)


def has_spec(worktree: str) -> bool:
    """Check if a spec file exists in the worktree."""
    wt = Path(worktree)
    specs_dir = wt / "specs"
    if specs_dir.exists():
        return any(f.suffix == ".md" for f in specs_dir.iterdir())
    return (wt / "SPEC.md").exists()


def has_pr(worktree: str) -> str | None:
    """Check if the worktree's branch has an open PR. Returns PR URL or None."""
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    result = subprocess.run(
        [gh, "pr", "view", "--json", "url,state"],
        cwd=worktree,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    import json
    try:
        data = json.loads(result.stdout)
        if data.get("state") in ("OPEN", "MERGED"):
            return data.get("url")
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def is_pr_merged(worktree: str) -> bool:
    """Check if the worktree's branch PR was merged."""
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    result = subprocess.run(
        [gh, "pr", "view", "--json", "state"],
        cwd=worktree,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    import json
    try:
        return json.loads(result.stdout).get("state") == "MERGED"
    except (json.JSONDecodeError, ValueError):
        return False


def has_question(worktree: str) -> str | None:
    """Check if the agent left a question. Returns question text or None."""
    qf = Path(worktree) / ".dispatch-question.md"
    if qf.exists():
        return qf.read_text().strip() or None
    return None
