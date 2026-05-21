"""Execute tmux actions output by the manager.

The manager handles Linear, GitHub, and Slack directly via tools.
This executor only handles tmux operations the manager can't do from
inside its own session: spawning engineers, injecting into sessions,
answering questions, killing sessions.
"""

import logging
import time
from pathlib import Path

from modastack.config import GlobalConfig
from modastack.session import (
    spawn_session, inject, inject_skill, answer_question,
    kill_session, session_exists,
)
from modastack.state import StateStore

log = logging.getLogger(__name__)

MEMORY_PATH = Path.home() / ".modastack" / "manager" / "memory.md"

# Tracks "Thinking..." placeholders: channel_id → message ts
_pending_placeholders: dict[str, str] = {}


def _get_slack_token() -> str:
    return GlobalConfig.load().slack_bot_token


async def post_thinking_placeholder(channel_id: str) -> None:
    """Post a 'Thinking...' message and track it for later update."""
    token = _get_slack_token()
    if not token or not channel_id:
        return
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id, "text": "_Thinking..._"},
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            _pending_placeholders[channel_id] = resp.json()["ts"]


async def execute_actions(actions: list[dict]) -> dict:
    """Execute tmux actions. Returns summary."""
    summary = {"executed": 0, "skipped": 0, "errors": 0}
    state = StateStore()

    for action in actions:
        action_type = action.get("type", "")
        try:
            if action_type == "no_action":
                log.info(f"Manager: no action — {action.get('reason', '')}")
                continue

            elif action_type in ("spawn_worker", "spawn_task"):
                iid = action.get("issue_id") or action.get("task_id", f"task-{int(time.time())}")
                repo = action.get("repo", "")
                title = action.get("title", iid)
                if state.is_tracked(iid):
                    log.info(f"Manager: {iid} already tracked, skipping spawn")
                    summary["skipped"] += 1
                    continue
                if not repo:
                    log.warning(f"Manager: spawn missing repo for {iid}")
                    summary["errors"] += 1
                    continue

                ok = spawn_session(iid, cwd=repo)
                if not ok:
                    summary["errors"] += 1
                    continue

                instructions = action.get("instructions", "")
                if action_type == "spawn_worker" and not instructions:
                    inject_skill(iid, "pickup",
                                 f"{iid} -- {title} "
                                 f"Linear UUID: {action.get('linear_id', '')}")
                else:
                    inject(iid, instructions or title)

                state.track(issue_id=iid, repo_path=repo,
                            title=title, worktree=repo,
                            linear_issue_id=action.get("linear_id"))

                log.info(f"Manager: spawned {'task' if action_type == 'spawn_task' else 'worker'} for {iid}: {title[:60]}")
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

            elif action_type == "update_memory":
                memory = action.get("memory", "")
                if memory:
                    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                    MEMORY_PATH.write_text(memory)
                    log.info(f"Manager: memory updated ({len(memory)} chars)")
                    summary["executed"] += 1

            elif action_type in ("move_linear_issue", "comment_linear", "send_slack"):
                # Manager handles these directly via tools now
                log.info(f"Manager: {action_type} — handled by manager session directly")
                continue

            else:
                log.warning(f"Manager: unknown action type '{action_type}'")
                summary["errors"] += 1

        except Exception as e:
            log.error(f"Manager: action {action_type} failed: {e}")
            summary["errors"] += 1

    return summary
