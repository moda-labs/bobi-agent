"""Integration test: full dispatch loop against real Linear + real Claude.

Creates a trivial issue in Linear, waits for dispatch to spec it,
approves the spec, waits for implementation PR, then cleans up.

Requires:
- LINEAR_API_KEY env var (or in ~/.dispatch/credentials.yaml)
- claude CLI authenticated
- dispatch daemon running

Run with: pytest tests/integration/ -v -s --timeout=300
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

# Test configuration
TEAM_KEY = "AGD"  # agentd's own Linear team
TRIGGER_LABEL = "agent"  # Same label dispatch watches
LINEAR_API = "https://api.linear.app/graphql"
POLL_INTERVAL = 10  # seconds between checks
MAX_WAIT_SPEC = 180  # 3 minutes for spec phase
MAX_WAIT_IMPL = 300  # 5 minutes for implementation
REPO_PATH = Path(__file__).parent.parent.parent  # agentd root


def get_api_key() -> str:
    """Get Linear API key from env or credentials."""
    key = os.environ.get("LINEAR_API_KEY")
    if key:
        return key

    creds_file = Path.home() / ".dispatch" / "credentials.yaml"
    if creds_file.exists():
        import yaml
        creds = yaml.safe_load(creds_file.read_text()) or {}
        for name, vals in creds.items():
            if vals.get("linear_api_key"):
                return vals["linear_api_key"]

    pytest.skip("No LINEAR_API_KEY available")


def linear_request(api_key: str, query: str, variables: dict = None) -> dict:
    """Make a Linear GraphQL request."""
    import truststore
    truststore.inject_into_ssl()

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    async def _req():
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                LINEAR_API,
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json=payload,
            )
            return resp.json()

    import asyncio
    return asyncio.run(_req())


@pytest.fixture
def api_key():
    return get_api_key()


@pytest.fixture
def test_issue(api_key):
    """Create a test issue and clean it up after."""
    # Get team ID
    data = linear_request(api_key, f'''{{
        teams(filter: {{ key: {{ eq: "{TEAM_KEY}" }} }}) {{
            nodes {{ id }}
        }}
    }}''')
    team_id = data["data"]["teams"]["nodes"][0]["id"]

    # Get the trigger label ID
    data = linear_request(api_key, f'''{{
        issueLabels(filter: {{ name: {{ eq: "{TRIGGER_LABEL}" }} }}) {{
            nodes {{ id }}
        }}
    }}''')
    label_ids = [l["id"] for l in data["data"]["issueLabels"]["nodes"]]

    # Create the issue
    title = f"[TEST] Add timestamp comment to README ({int(time.time())})"
    description = "Add a comment at the bottom of README.md with the current UTC timestamp. This is an automated test issue."

    data = linear_request(api_key, '''
        mutation($teamId: String!, $title: String!, $description: String!, $labelIds: [String!]) {
            issueCreate(input: {
                teamId: $teamId
                title: $title
                description: $description
                labelIds: $labelIds
            }) {
                success
                issue { id identifier }
            }
        }
    ''', {
        "teamId": team_id,
        "title": title,
        "description": description,
        "labelIds": label_ids,
    })

    issue = data["data"]["issueCreate"]["issue"]
    print(f"\nCreated test issue: {issue['identifier']}")

    # Move to Todo (Linear defaults to Backlog)
    states_data = linear_request(api_key, f'''{{
        teams(filter: {{ key: {{ eq: "{TEAM_KEY}" }} }}) {{
            nodes {{ states {{ nodes {{ id type }} }} }}
        }}
    }}''')
    todo_id = next(
        s["id"] for s in states_data["data"]["teams"]["nodes"][0]["states"]["nodes"]
        if s["type"] == "unstarted"
    )
    linear_request(api_key, f'''
        mutation {{ issueUpdate(id: "{issue['id']}", input: {{ stateId: "{todo_id}" }}) {{ success }} }}
    ''')
    print(f"  Moved to Todo")

    yield {
        "id": issue["id"],
        "identifier": issue["identifier"],
        "title": title,
    }

    # Cleanup: cancel the issue
    linear_request(api_key, f'''
        mutation {{
            issueUpdate(id: "{issue['id']}", input: {{ stateId: "29f7cbf5-107a-4235-bdf9-66f2977b8138" }}) {{
                success
            }}
        }}
    ''')
    print(f"Cleaned up: {issue['identifier']} → Canceled")

    # Cleanup: remove worktree if created
    slug = title.lower()
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', slug)[:50].strip('-')
    worktree_dir = REPO_PATH / "worktrees" / f"{issue['identifier'].lower()}-{slug}"
    if worktree_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(REPO_PATH),
            capture_output=True,
        )
        print(f"Cleaned up worktree: {worktree_dir.name}")


def wait_for_state(api_key: str, issue_id: str, target_states: list[str],
                   timeout: int = 120, label: str = "") -> str:
    """Poll until the issue reaches one of the target states."""
    start = time.time()
    while time.time() - start < timeout:
        data = linear_request(api_key, f'''{{
            issue(id: "{issue_id}") {{
                state {{ name }}
                comments {{ nodes {{ body }} }}
            }}
        }}''')
        issue = data.get("data", {}).get("issue", {})
        state_name = issue.get("state", {}).get("name", "")

        if state_name in target_states:
            print(f"  → Reached '{state_name}' ({label})")
            return state_name

        time.sleep(POLL_INTERVAL)

    pytest.fail(f"Timed out waiting for {target_states} (was: {state_name}, {label})")


def approve_spec(api_key: str, issue_id: str):
    """Post 'approved' comment on the issue."""
    linear_request(api_key, '''
        mutation($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
    ''', {"issueId": issue_id, "body": "approved"})
    print("  → Posted 'approved'")


class TestFullLoop:
    """End-to-end test of the dispatch lifecycle."""

    @pytest.mark.timeout(600)
    def test_spec_phase(self, api_key, test_issue):
        """Test that dispatch picks up an issue and writes a spec."""
        print(f"\nWaiting for dispatch to pick up {test_issue['identifier']}...")

        # Wait for it to leave Todo (picked up by dispatch)
        state = wait_for_state(
            api_key, test_issue["id"],
            ["Planning", "Design Review", "Implementing"],
            timeout=MAX_WAIT_SPEC,
            label="spec pickup",
        )

        assert state in ["Planning", "Design Review", "Implementing"]

    @pytest.mark.timeout(600)
    def test_full_cycle(self, api_key, test_issue):
        """Test the full spec → approve → implement cycle."""
        print(f"\nWaiting for spec phase on {test_issue['identifier']}...")

        # Wait for Design Review (spec complete)
        wait_for_state(
            api_key, test_issue["id"],
            ["Design Review"],
            timeout=MAX_WAIT_SPEC,
            label="spec complete",
        )

        # Approve the spec
        time.sleep(5)  # Let the comment post settle
        approve_spec(api_key, test_issue["id"])

        # Wait for implementation to start
        print("  Waiting for implementation...")
        wait_for_state(
            api_key, test_issue["id"],
            ["Implementing", "In Review"],
            timeout=MAX_WAIT_IMPL,
            label="implementation",
        )

        print(f"  ✓ Full cycle completed for {test_issue['identifier']}")
