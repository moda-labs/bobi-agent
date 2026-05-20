"""Execute actions output by the manager.

The manager outputs a JSON array of actions. This module parses them
and calls the appropriate dispatch/session functions.
"""

import asyncio
import json
import logging
from pathlib import Path

from dispatch.config import GlobalConfig, RepoConfig
from dispatch.linear_api import get_state_ids, move_issue, add_comment
from dispatch.session import (
    spawn_session, inject, inject_skill, answer_question,
    kill_session, session_exists,
)
from dispatch.state import StateStore

log = logging.getLogger(__name__)

MEMORY_PATH = Path.home() / ".dispatch" / "manager" / "memory.md"


async def execute_actions(actions: list[dict]) -> dict:
    """Execute a list of manager actions. Returns summary."""
    summary = {"executed": 0, "skipped": 0, "errors": 0}
    state = StateStore()
    global_config = GlobalConfig.load()

    # Build API key + repo config lookup by project
    api_keys = {}
    repo_configs = {}
    state_id_cache = {}
    for repo_path in global_config.repos:
        if not repo_path.exists():
            continue
        try:
            rc = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            continue
        creds = rc.get_credentials()
        key = creds.get("linear_api_key") or global_config.linear_api_key
        if key:
            api_keys[rc.linear_project] = key
            api_keys[str(repo_path)] = key
            repo_configs[rc.linear_project] = rc

    async def get_states(project_or_repo: str) -> dict:
        if project_or_repo not in state_id_cache:
            key = api_keys.get(project_or_repo, "")
            if not key:
                return {}
            # Guess the team key from the issue ID prefix or use the project
            team_key = project_or_repo.split("/")[-1].upper() if "/" in project_or_repo else project_or_repo
            state_id_cache[project_or_repo] = await get_state_ids(key, team_key)
        return state_id_cache[project_or_repo]

    for action in actions:
        action_type = action.get("type", "")
        try:
            if action_type == "no_action":
                log.info(f"Manager: no action — {action.get('reason', '')}")
                continue

            elif action_type == "spawn_worker":
                iid = action["issue_id"]
                repo = action.get("repo", "")
                if state.is_tracked(iid):
                    log.info(f"Manager: {iid} already tracked, skipping spawn")
                    summary["skipped"] += 1
                    continue
                if not repo:
                    log.warning(f"Manager: spawn_worker missing repo for {iid}")
                    summary["errors"] += 1
                    continue

                ok = spawn_session(iid, cwd=repo)
                if ok:
                    inject_skill(iid, "pickup",
                                 f"{iid} -- {action.get('title', '')} "
                                 f"Linear UUID: {action.get('linear_id', '')}")
                    state.track(issue_id=iid, repo_path=repo,
                                title=action.get("title", iid),
                                worktree=repo,
                                linear_issue_id=action.get("linear_id"))

                    # Move to In Progress
                    project = iid.split("-")[0]
                    api_key = api_keys.get(project, "")
                    if api_key:
                        states = await get_states(project)
                        if "In Progress" in states and action.get("linear_id"):
                            await move_issue(api_key, action["linear_id"], states["In Progress"])
                        if action.get("linear_id"):
                            await add_comment(api_key, action["linear_id"], "Picked up by modabot.")

                    log.info(f"Manager: spawned worker for {iid}")
                    summary["executed"] += 1

            elif action_type == "inject_into_worker":
                iid = action["issue_id"]
                msg = action.get("message", "")
                if session_exists(iid) and msg:
                    inject(iid, msg)
                    state.touch(iid)
                    log.info(f"Manager: injected into {iid}")
                    summary["executed"] += 1
                else:
                    summary["skipped"] += 1

            elif action_type == "answer_worker_question":
                iid = action["issue_id"]
                if session_exists(iid):
                    answer_question(iid,
                                    choice=action.get("choice"),
                                    text=action.get("text"))
                    state.touch(iid)
                    log.info(f"Manager: answered question for {iid}")
                    summary["executed"] += 1
                else:
                    summary["skipped"] += 1

            elif action_type == "kill_worker":
                iid = action["issue_id"]
                if session_exists(iid):
                    kill_session(iid)
                state.remove(iid)
                log.info(f"Manager: killed worker {iid}")
                summary["executed"] += 1

            elif action_type == "route_skill":
                iid = action["issue_id"]
                skill = action.get("skill", "")
                if session_exists(iid) and skill:
                    inject_skill(iid, skill, f"Issue: {iid}")
                    agent = state.get(iid)
                    if agent:
                        state.set_phase(iid, f"_working_{skill}")
                    state.touch(iid)
                    log.info(f"Manager: routed /{skill} to {iid}")
                    summary["executed"] += 1
                else:
                    summary["skipped"] += 1

            elif action_type == "move_linear_issue":
                iid = action["issue_id"]
                target = action.get("state", "")
                project = iid.split("-")[0]
                api_key = api_keys.get(project, "")
                linear_id = action.get("linear_id") or (state.get(iid).linear_issue_id if state.get(iid) else None)
                if not api_key:
                    log.warning(f"Manager: no API key for project {project}")
                    summary["errors"] += 1
                    continue
                if not linear_id:
                    log.warning(f"Manager: no linear_id for {iid}")
                    summary["errors"] += 1
                    continue
                states = await get_states(project)
                if target not in states:
                    log.warning(f"Manager: state '{target}' not found for {project}")
                    summary["errors"] += 1
                    continue
                await move_issue(api_key, linear_id, states[target])
                log.info(f"Manager: moved {iid} → {target}")
                summary["executed"] += 1
                if target == "Done":
                    if session_exists(iid):
                        kill_session(iid)
                    state.remove(iid)

            elif action_type == "comment_linear":
                iid = action["issue_id"]
                body = action.get("body", "")
                project = iid.split("-")[0]
                api_key = api_keys.get(project, "")
                linear_id = action.get("linear_id") or (state.get(iid).linear_issue_id if state.get(iid) else None)
                if api_key and body and linear_id:
                    await add_comment(api_key, linear_id, body)
                    log.info(f"Manager: commented on {iid}")
                    summary["executed"] += 1
                else:
                    log.warning(f"Manager: comment_linear failed for {iid} (key={bool(api_key)}, id={bool(linear_id)}, body={bool(body)})")
                    summary["errors"] += 1

            elif action_type == "send_slack":
                channel = action.get("channel", "")
                channel_id = action.get("channel_id", "")
                msg = action.get("message", "")

                from dispatch.config import Credentials
                slack_token = ""
                for name in Credentials.load().list_names():
                    t = Credentials.load().get(name).get("slack_bot_token", "")
                    if t:
                        slack_token = t
                        break
                if not slack_token:
                    slack_token = GlobalConfig.load().slack_bot_token or ""

                if slack_token and (channel or channel_id):
                    import httpx
                    target = channel_id or channel
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            "https://slack.com/api/chat.postMessage",
                            headers={"Authorization": f"Bearer {slack_token}"},
                            json={"channel": target, "text": msg},
                        )
                        if resp.status_code == 200 and resp.json().get("ok"):
                            log.info(f"Manager: sent to Slack {target}: {msg[:80]}")
                            summary["executed"] += 1
                        else:
                            log.warning(f"Manager: Slack send failed: {resp.json().get('error', 'unknown')}")
                            summary["errors"] += 1
                else:
                    log.info(f"Manager: [SLACK {channel}] {msg} (no token, logged only)")
                    summary["executed"] += 1

            elif action_type == "update_memory":
                memory = action.get("memory", "")
                if memory:
                    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                    MEMORY_PATH.write_text(memory)
                    log.info(f"Manager: memory updated ({len(memory)} chars)")
                    summary["executed"] += 1

            else:
                log.warning(f"Manager: unknown action type '{action_type}'")
                summary["errors"] += 1

        except Exception as e:
            log.error(f"Manager: action {action_type} failed: {e}")
            summary["errors"] += 1

    return summary
