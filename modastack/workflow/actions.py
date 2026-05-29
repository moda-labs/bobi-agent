"""Action handlers — deterministic operations the engine can execute.

Each handler takes resolved params and returns an outputs dict.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable

log = logging.getLogger(__name__)


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


def _ticket_move(params: dict) -> dict:
    issue_id = params.get("issue_id", "")
    state = params.get("state", "")
    repo = params.get("repo", "")

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
    return repo


def _session_spawn(params: dict) -> dict:
    from modastack.session import sync_main_branch
    issue_id = params.get("issue_id", "").lstrip("#")
    repo = params.get("repo", "")
    cwd = _resolve_repo_path(repo)

    from pathlib import Path
    ok = sync_main_branch(Path(cwd))
    log.info(f"Prepared repo {cwd} for sub-agent (issue {issue_id})")
    return {"ok": ok, "cwd": cwd}


def build_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register("slack.post", _slack_post)
    registry.register("ticket.move", _ticket_move)
    registry.register("ticket.comment", _ticket_comment)
    registry.register("session.spawn", _session_spawn)
    return registry
