"""Main dispatch engine.

Linear is the source of truth. Each cycle:
1. Poll Linear for all issues in active states
2. Reconcile: kill agents for issues that moved to terminal states
3. Stall detection: kill agents with no activity for 5 minutes
4. Detect completed agents and check for user replies to re-spawn
5. Dispatch new agents for Todo issues with the trigger label
"""

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import get_latest_human_comment
from .dispatcher import spawn_agent, read_agent_output
from .scanner import scan_linear_all_active, WorkItem, WorkSource
from .state import StateStore

log = logging.getLogger(__name__)

STALL_TIMEOUT_SECONDS = 300  # 5 minutes without activity → kill
MAX_ATTEMPTS = 3


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _kill_agent(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


async def run_cycle() -> dict:
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"scanned": 0, "dispatched": 0, "completed": 0, "killed": 0, "respawned": 0}

    for repo_path in global_config.repos:
        if not repo_path.exists():
            continue

        try:
            repo_config = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            continue

        creds = repo_config.get_credentials()
        api_key = creds.get("linear_api_key") or global_config.linear_api_key
        if not api_key:
            continue

        # 1. Poll Linear for ALL issues in active + terminal states
        issues_by_state = await scan_linear_all_active(api_key, repo_config)

        # Build lookup: issue_id → (state_name, issue_data)
        linear_state = {}
        for state_name, issues in issues_by_state.items():
            for issue in issues:
                linear_state[issue["identifier"]] = (state_name, issue)
                summary["scanned"] += 1

        # 2. Reconcile running agents against Linear
        for agent in state.agents_for_repo(str(repo_path)):
            linear_info = linear_state.get(agent.issue_id)

            if linear_info is None:
                # Issue no longer visible (deleted or moved out of scope)
                if _is_alive(agent.pid):
                    _kill_agent(agent.pid)
                    log.info(f"Killed {agent.issue_id}: issue no longer in scope")
                    summary["killed"] += 1
                state.remove(agent.issue_id)
                continue

            issue_state, _ = linear_info
            terminal = {"Done", "Canceled", "Cancelled", "Duplicate"}
            if issue_state in terminal:
                if _is_alive(agent.pid):
                    _kill_agent(agent.pid)
                    log.info(f"Killed {agent.issue_id}: issue moved to {issue_state}")
                    summary["killed"] += 1
                state.remove(agent.issue_id)
                continue

        # 3. Stall detection
        for agent in state.agents_for_repo(str(repo_path)):
            if not _is_alive(agent.pid):
                continue
            elapsed = time.time() - agent.last_activity_at
            if elapsed > STALL_TIMEOUT_SECONDS:
                _kill_agent(agent.pid)
                log.warning(f"Killed stalled agent {agent.issue_id} ({int(elapsed)}s without activity)")
                state.remove(agent.issue_id)
                summary["killed"] += 1

        # 4. Detect completed agents — check for user replies to re-spawn
        for agent in list(state.agents_for_repo(str(repo_path))):
            if _is_alive(agent.pid):
                # Update activity if worktree has recent changes
                worktree = Path(agent.worktree)
                if worktree.exists():
                    progress = worktree / ".dispatch-progress.md"
                    dispatch_state = worktree / ".dispatch" / "state.md"
                    for f in [progress, dispatch_state]:
                        if f.exists() and f.stat().st_mtime > agent.last_activity_at:
                            state.touch(agent.issue_id)
                            break
                continue

            # Agent exited — check output
            result = read_agent_output(agent.issue_id)
            if result.get("status") == "failed" and agent.attempts < MAX_ATTEMPTS:
                log.info(f"Agent {agent.issue_id} failed (attempt {agent.attempts}), will retry")
                state.remove(agent.issue_id)
                continue

            # Check for user reply on Linear (triggers re-spawn)
            if agent.linear_issue_id:
                reply = await get_latest_human_comment(api_key, agent.linear_issue_id)
                if reply:
                    # Re-spawn with reply as context
                    work_item = WorkItem(
                        id=agent.issue_id,
                        source=WorkSource.LINEAR,
                        title=agent.title,
                        body="",
                        repo_config=repo_config,
                        labels=[],
                        linear_issue_id=agent.linear_issue_id,
                    )
                    spawned = spawn_agent(work_item, state, user_reply=reply)
                    if spawned:
                        summary["respawned"] += 1
                        log.info(f"Re-spawned {agent.issue_id}: user replied")
                    continue

            # No reply — just mark completed
            summary["completed"] += 1
            state.remove(agent.issue_id)
            log.info(f"Completed {agent.issue_id}")

        # 5. Dispatch new agents for Todo issues with trigger label
        todo_issues = issues_by_state.get("Todo", [])
        for issue_data in todo_issues:
            issue_id = issue_data["identifier"]
            labels = [l["name"] for l in issue_data.get("labels", {}).get("nodes", [])]

            if not any(t in labels for t in repo_config.trigger_labels):
                continue
            if any(s in labels for s in repo_config.skip_labels):
                continue
            if state.is_tracked(issue_id):
                continue

            # Check parallel limit
            if len(state.agents_for_repo(str(repo_path))) >= repo_config.max_parallel:
                break

            work_item = WorkItem(
                id=issue_id,
                source=WorkSource.LINEAR,
                title=issue_data["title"],
                body=issue_data.get("description") or "",
                repo_config=repo_config,
                labels=labels,
                linear_issue_id=issue_data["id"],
            )

            spawned = spawn_agent(work_item, state)
            if spawned:
                summary["dispatched"] += 1
                log.info(f"Dispatched {issue_id}: {issue_data['title']} (PID {spawned['pid']})")

    return summary


def run() -> dict:
    return asyncio.run(run_cycle())
