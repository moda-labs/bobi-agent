"""Main dispatch engine.

ALL Linear state transitions happen here. The agent just does work and exits.

Each cycle:
1. Poll Linear for all issues
2. Reconcile: kill agents for terminal issues
3. Stall detection: kill agents with no activity
4. State transitions: move issues based on worktree state
5. Re-spawn: detect human replies and re-dispatch
6. Dispatch: pick up new Todo issues
"""

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import get_latest_human_reply_after_agent
from .dispatcher import spawn_agent, read_agent_output
from .linear_state import (
    get_state_ids, move_issue, add_comment,
    has_spec, has_pr, is_pr_merged, has_question,
)
from .scanner import scan_linear_all_active, WorkItem, WorkSource
from .state import StateStore

log = logging.getLogger(__name__)

STALL_TIMEOUT_SECONDS = 600  # 10 minutes
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

        # Cache state IDs for this team
        state_ids = await get_state_ids(api_key, repo_config.linear_project)

        # 1. Poll Linear
        issues_by_state = await scan_linear_all_active(api_key, repo_config)

        linear_state = {}
        for state_name, issues in issues_by_state.items():
            for issue in issues:
                linear_state[issue["identifier"]] = (state_name, issue)
                summary["scanned"] += 1

        # 2. Reconcile — kill agents for terminal issues
        terminal = {"Done", "Canceled", "Cancelled", "Duplicate"}
        for agent in list(state.agents_for_repo(str(repo_path))):
            info = linear_state.get(agent.issue_id)
            if info is None or info[0] in terminal:
                if _is_alive(agent.pid):
                    _kill_agent(agent.pid)
                    summary["killed"] += 1
                state.remove(agent.issue_id)

        # 3. Stall detection + live state updates for running agents
        for agent in list(state.agents_for_repo(str(repo_path))):
            if not _is_alive(agent.pid):
                continue

            wt = Path(agent.worktree)
            linear_info = linear_state.get(agent.issue_id)
            if not linear_info:
                continue
            current_state = linear_info[0]
            linear_id = linear_info[1]["id"]

            # Update activity from worktree file changes
            for f in [wt / ".dispatch-progress.md", wt / ".dispatch" / "state.md"]:
                if f.exists() and f.stat().st_mtime > agent.last_activity_at:
                    state.touch(agent.issue_id)
                    break

            # Live state: detect if agent is planning or implementing
            import subprocess
            diff_result = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=str(wt), capture_output=True, text=True,
            )
            changed_files = diff_result.stdout.strip().splitlines() if diff_result.returncode == 0 else []
            has_code_changes = any(
                not f.startswith("specs/") and not f.startswith(".dispatch")
                for f in changed_files
            )

            if has_code_changes and current_state == "Planning" and "Implementing" in state_ids:
                await move_issue(api_key, linear_id, state_ids["Implementing"])
                log.info(f"{agent.issue_id}: Planning → Implementing (code changes detected)")

            # Kill if stalled
            elapsed = time.time() - state.get(agent.issue_id).last_activity_at
            if elapsed > STALL_TIMEOUT_SECONDS:
                _kill_agent(agent.pid)
                log.warning(f"Stalled {agent.issue_id} ({int(elapsed)}s)")
                state.remove(agent.issue_id)
                summary["killed"] += 1

        # 4. State transitions for exited agents
        for agent in list(state.agents_for_repo(str(repo_path))):
            if _is_alive(agent.pid):
                continue

            linear_info = linear_state.get(agent.issue_id)
            if not linear_info:
                state.remove(agent.issue_id)
                continue

            current_state, issue_data = linear_info
            linear_id = issue_data["id"]
            wt = agent.worktree

            # Check for failure
            result = read_agent_output(agent.issue_id)
            if result.get("status") == "failed":
                if agent.attempts < MAX_ATTEMPTS:
                    log.info(f"Failed {agent.issue_id} (attempt {agent.attempts}), retrying")
                    state.remove(agent.issue_id)
                    # Move back to Todo for retry
                    if "Todo" in state_ids:
                        await move_issue(api_key, linear_id, state_ids["Todo"])
                    continue
                else:
                    log.warning(f"Giving up on {agent.issue_id}")
                    if "Blocked" in state_ids:
                        await move_issue(api_key, linear_id, state_ids["Blocked"])
                        await add_comment(api_key, linear_id,
                            f"🤖 **Failed after {agent.attempts} attempts.** Needs human help.")
                    state.remove(agent.issue_id)
                    continue

            # Agent succeeded — determine the right state transition
            question = has_question(wt)
            spec_exists = has_spec(wt)
            pr_url = has_pr(wt)
            merged = is_pr_merged(wt) if pr_url else False

            if question:
                # Agent has a question → Blocked
                if "Blocked" in state_ids and current_state != "Blocked":
                    await move_issue(api_key, linear_id, state_ids["Blocked"])
                    await add_comment(api_key, linear_id, f"🤖 **Question:**\n\n{question}")
                    log.info(f"{agent.issue_id} → Blocked (question)")

            elif merged:
                # PR merged → Done
                if "Done" in state_ids and current_state != "Done":
                    await move_issue(api_key, linear_id, state_ids["Done"])
                    await add_comment(api_key, linear_id, "🤖 **PR merged.** Issue complete.")
                    log.info(f"{agent.issue_id} → Done (merged)")

            elif pr_url:
                # PR exists → In Review
                if "In Review" in state_ids and current_state != "In Review":
                    await move_issue(api_key, linear_id, state_ids["In Review"])
                    await add_comment(api_key, linear_id, f"🤖 **Ready for review.**\n\nPR: {pr_url}")
                    log.info(f"{agent.issue_id} → In Review (PR: {pr_url})")

            elif spec_exists and current_state in ("Todo", "Planning"):
                # Spec written but no PR yet → Design Review
                if "Design Review" in state_ids:
                    await move_issue(api_key, linear_id, state_ids["Design Review"])
                    await add_comment(api_key, linear_id,
                        "🤖 **Spec ready for review.** Check the draft PR or worktree specs/ directory.\n\n"
                        "**Reply 'approved' to start implementation.**")
                    log.info(f"{agent.issue_id} → Design Review (spec ready)")

            # Clean up from state
            summary["completed"] += 1
            state.remove(agent.issue_id)

        # 5. Re-spawn for issues awaiting action
        for review_state in ["Design Review", "In Review", "Blocked"]:
            for issue_data in issues_by_state.get(review_state, []):
                issue_id = issue_data["identifier"]
                if state.is_tracked(issue_id):
                    continue

                labels = [l["name"] for l in issue_data.get("labels", {}).get("nodes", [])]
                if not any(t in labels for t in repo_config.trigger_labels):
                    continue

                reply = await get_latest_human_reply_after_agent(api_key, issue_data["id"])
                if not reply:
                    continue

                # Move to appropriate active state before spawning
                if review_state == "Design Review" and "Implementing" in state_ids:
                    await move_issue(api_key, issue_data["id"], state_ids["Implementing"])
                elif review_state in ("In Review", "Blocked") and "Implementing" in state_ids:
                    await move_issue(api_key, issue_data["id"], state_ids["Implementing"])

                work_item = WorkItem(
                    id=issue_id,
                    source=WorkSource.LINEAR,
                    title=issue_data["title"],
                    body=issue_data.get("description") or "",
                    repo_config=repo_config,
                    labels=labels,
                    linear_issue_id=issue_data["id"],
                )
                spawned = spawn_agent(work_item, state, user_reply=reply)
                if spawned:
                    summary["respawned"] += 1
                    log.info(f"Re-spawned {issue_id}: user replied in {review_state}")

        # 6. Check In Review issues for merged PRs (no agent needed)
        for issue_data in issues_by_state.get("In Review", []):
            issue_id = issue_data["identifier"]
            if state.is_tracked(issue_id):
                continue
            # Find the worktree for this issue
            worktrees_dir = repo_path / "worktrees"
            if not worktrees_dir.exists():
                continue
            for wt in worktrees_dir.iterdir():
                if wt.is_dir() and wt.name.startswith(issue_id.lower()):
                    if is_pr_merged(str(wt)):
                        if "Done" in state_ids:
                            await move_issue(api_key, issue_data["id"], state_ids["Done"])
                            await add_comment(api_key, issue_data["id"], "🤖 **PR merged.** Issue complete.")
                            log.info(f"{issue_id} → Done (PR merged)")
                    break

        # 7. Dispatch new Todo issues
        for issue_data in issues_by_state.get("Todo", []):
            issue_id = issue_data["identifier"]
            labels = [l["name"] for l in issue_data.get("labels", {}).get("nodes", [])]

            if not any(t in labels for t in repo_config.trigger_labels):
                continue
            if any(s in labels for s in repo_config.skip_labels):
                continue
            if state.is_tracked(issue_id):
                continue
            if len(state.agents_for_repo(str(repo_path))) >= repo_config.max_parallel:
                break

            # Move to Planning before dispatching
            if "Planning" in state_ids:
                await move_issue(api_key, issue_data["id"], state_ids["Planning"])

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
