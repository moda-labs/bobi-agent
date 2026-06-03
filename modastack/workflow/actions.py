"""Action handlers — deterministic operations the engine can execute.

Each handler takes resolved params and returns an outputs dict.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"


class ActionRegistry:

    def __init__(self):
        self._handlers: dict[str, Callable] = {}

    def register(self, name: str, handler: Callable):
        self._handlers[name] = handler

    def execute(self, action_name: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(action_name)
        if not handler:
            raise ValueError(f"Unknown action: {action_name}")
        return handler(params)


def _slack_post(params: dict) -> dict:
    from modastack.config import GlobalConfig
    config = GlobalConfig.load()
    token = config.slack_bot_token

    if not token:
        log.warning("No Slack bot token configured — skipping slack.post")
        return {"ok": False, "error": "no_token"}

    channel = params.get("channel_id", "")
    text = params.get("text", "")
    thread_ts = params.get("thread_ts")

    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    result = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=10,
    )

    try:
        resp = json.loads(result.stdout)
        ts = resp.get("ts", "")
        ok = resp.get("ok", False)
        if not ok:
            log.warning(f"Slack post failed: {resp.get('error', 'unknown')}")
        return {"ts": ts, "ok": ok}
    except (json.JSONDecodeError, AttributeError):
        log.warning(f"Slack post returned invalid response: {result.stdout[:200]}")
        return {"ok": False, "error": "invalid_response"}


def _resolve_task_tracking(repo: str) -> tuple[str, dict[str, str]]:
    """Determine whether a repo uses GitHub Issues or Linear.

    Returns (system, credentials) where system is 'github-issues' or 'linear'.
    """
    from modastack.config import GlobalConfig, RepoConfig

    config = GlobalConfig.load()
    repo_name = repo.split("/")[-1] if "/" in repo else repo

    for repo_path in config.repos:
        if repo_path.name != repo_name:
            continue
        try:
            repo_config = RepoConfig.from_file(repo_path)
            creds = repo_config.get_credentials()
            return repo_config.task_tracking, creds
        except FileNotFoundError:
            break

    return "github-issues", {}


def _linear_graphql(api_key: str, query: str, variables: dict | None = None) -> dict:
    """Execute a Linear GraphQL query/mutation."""
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    resp = httpx.post(
        LINEAR_API,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if resp.status_code != 200:
        log.warning(f"Linear API returned {resp.status_code}: {resp.text[:200]}")
        return {}
    return resp.json()


def _linear_find_state_id(api_key: str, team_key: str, state_name: str) -> str | None:
    """Look up a Linear workflow state ID by name."""
    data = _linear_graphql(api_key, f'''{{
        teams(filter: {{ key: {{ eq: "{team_key}" }} }}) {{
            nodes {{ states {{ nodes {{ id name }} }} }}
        }}
    }}''')
    teams = data.get("data", {}).get("teams", {}).get("nodes", [])
    if not teams:
        return None
    for state in teams[0].get("states", {}).get("nodes", []):
        if state["name"].lower() == state_name.lower():
            return state["id"]
    return None


def _linear_resolve_issue_id(api_key: str, identifier: str) -> str | None:
    """Resolve a Linear identifier (e.g. FAM-123) to its UUID."""
    data = _linear_graphql(api_key, f'''{{
        issue(id: "{identifier}") {{ id }}
    }}''')
    return data.get("data", {}).get("issue", {}).get("id")


def _ticket_move(params: dict) -> dict:
    issue_id = params.get("issue_id", "")
    state = params.get("state", "")
    repo = params.get("repo", "")
    linear_id = params.get("linear_id", "")

    system, creds = _resolve_task_tracking(repo)

    if system == "linear":
        api_key = creds.get("linear_api_key", "")
        if not api_key:
            log.warning("No Linear API key for repo %s — skipping ticket.move", repo)
            return {"ok": False, "error": "no_linear_api_key"}

        uuid = linear_id or _linear_resolve_issue_id(api_key, issue_id)
        if not uuid:
            log.warning("Could not resolve Linear issue %s", issue_id)
            return {"ok": False, "error": f"issue_not_found: {issue_id}"}

        team_key = issue_id.split("-")[0] if "-" in issue_id else ""

        if state == "Done":
            state_id = _linear_find_state_id(api_key, team_key, "Done")
        else:
            state_id = _linear_find_state_id(api_key, team_key, state)

        if not state_id:
            log.warning("State '%s' not found for team %s", state, team_key)
            return {"ok": False, "error": f"state_not_found: {state}"}

        data = _linear_graphql(api_key,
            'mutation($id: String!, $stateId: String!) { issueUpdate(id: $id, input: { stateId: $stateId }) { success } }',
            {"id": uuid, "stateId": state_id},
        )
        success = data.get("data", {}).get("issueUpdate", {}).get("success", False)
        return {"ok": success}

    # GitHub Issues path
    label_map = {
        "In Progress": ("status:todo", "status:in-progress"),
        "In Review": ("status:in-progress", "status:in-review"),
        "Blocked": ("status:in-progress", "status:blocked"),
    }

    if state == "Done":
        cmd = ["gh", "issue", "close", str(issue_id)]
        if repo:
            cmd += ["--repo", repo]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {"ok": result.returncode == 0}

    remove_label, add_label = label_map.get(state, ("", ""))
    if not add_label:
        return {"ok": False, "error": f"Unknown state: {state}"}

    cmd = ["gh", "issue", "edit", str(issue_id), "--add-label", add_label]
    if remove_label:
        cmd += ["--remove-label", remove_label]
    if repo:
        cmd += ["--repo", repo]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return {"ok": result.returncode == 0}


def _ticket_comment(params: dict) -> dict:
    issue_id = params.get("issue_id", "")
    body = params.get("body", "")
    repo = params.get("repo", "")
    linear_id = params.get("linear_id", "")

    system, creds = _resolve_task_tracking(repo)

    if system == "linear":
        api_key = creds.get("linear_api_key", "")
        if not api_key:
            log.warning("No Linear API key for repo %s — skipping ticket.comment", repo)
            return {"ok": False, "error": "no_linear_api_key"}

        uuid = linear_id or _linear_resolve_issue_id(api_key, issue_id)
        if not uuid:
            log.warning("Could not resolve Linear issue %s", issue_id)
            return {"ok": False, "error": f"issue_not_found: {issue_id}"}

        data = _linear_graphql(api_key,
            'mutation($issueId: String!, $body: String!) { commentCreate(input: { issueId: $issueId, body: $body }) { success } }',
            {"issueId": uuid, "body": body},
        )
        success = data.get("data", {}).get("commentCreate", {}).get("success", False)
        return {"ok": success}

    # GitHub Issues path
    cmd = ["gh", "issue", "comment", str(issue_id), "--body", body]
    if repo:
        cmd += ["--repo", repo]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return {"ok": result.returncode == 0}


def _resolve_repo_path(repo: str) -> str:
    """Resolve a repo identifier (org/name or path) to a local path."""
    from modastack.config import GlobalConfig
    config = GlobalConfig.load()
    repo_name = repo.split("/")[-1] if "/" in repo else repo
    for p in config.repos:
        if p.name == repo_name:
            return str(p)
    raise FileNotFoundError(
        f"Repo '{repo}' not registered with modastack. "
        f"Run: modastack setup <path-to-{repo_name}>"
    )


def _session_spawn(params: dict) -> dict:
    from modastack.session import sync_main_branch
    issue_id = params.get("issue_id", "").lstrip("#")
    repo = params.get("repo", "")
    cwd = _resolve_repo_path(repo)

    from pathlib import Path
    ok = sync_main_branch(Path(cwd))

    # Ensure worktree parent directory exists under modastack repo
    modastack_root = Path(__file__).parent.parent
    repo_name = Path(cwd).name
    worktree_parent = modastack_root / "worktrees" / repo_name
    worktree_parent.mkdir(parents=True, exist_ok=True)

    log.info(f"Prepared repo {cwd} for sub-agent (issue {issue_id})")
    return {"ok": ok, "cwd": cwd}


def build_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register("slack.post", _slack_post)
    registry.register("ticket.move", _ticket_move)
    registry.register("ticket.comment", _ticket_comment)
    registry.register("session.spawn", _session_spawn)
    return registry
