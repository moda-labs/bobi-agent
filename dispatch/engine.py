"""Main dispatch engine — the cron entrypoint."""

import asyncio
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import poll_blocked_sessions
from .scanner import scan_linear, scan_slack, WorkItem, WorkSource
from .state import StateStore, Status
from .dispatcher import spawn_agent, check_in_flight
from .reporter import (
    report_completion, report_failure,
    move_to_in_progress, move_to_in_review, add_comment,
)

log = logging.getLogger(__name__)


async def run_cycle() -> dict:
    """Run one dispatch cycle. Called by cron every N minutes.

    Returns a summary dict of what happened this cycle.
    """
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"scanned": 0, "dispatched": 0, "completed": 0, "failed": 0, "skipped": 0, "unblocked": 0}

    # 1. Check in-flight work first
    updates = await check_in_flight(state)
    for update in updates:
        item_id = update["id"]

        if update["status"] == "done":
            summary["completed"] += 1
            # Move Linear to In Review and comment
            tracked = state._items.get(item_id)
            if tracked and tracked.linear_issue_id:
                repo_path = Path(tracked.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        await move_to_in_review(api_key, tracked.linear_issue_id, repo_config.linear_project)
                        pr_text = f"\n\nPR: {tracked.pr_url}" if tracked.pr_url else ""
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Done.** Ready for review.{pr_text}")
                except FileNotFoundError:
                    pass

        elif update["status"] == "failed":
            summary["failed"] += 1
            tracked = state._items.get(item_id)
            if tracked and tracked.linear_issue_id:
                repo_path = Path(tracked.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        error_text = f"\n\nError: {tracked.error}" if tracked.error else ""
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Failed/stuck.** Needs human attention.{error_text}")
                except FileNotFoundError:
                    pass

    # 1b. Poll blocked sessions for user replies on Linear
    unblocked = await poll_blocked_sessions(global_config, state)
    summary["unblocked"] = len(unblocked)
    for item in unblocked:
        log.info(f"Unblocked {item['id']}: user replied on Linear")

    # 2. Scan for new work across all registered repos
    all_work: list[WorkItem] = []

    for repo_path in global_config.repos:
        if not repo_path.exists():
            log.warning(f"Repo path does not exist: {repo_path}")
            continue

        try:
            repo_config = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            log.debug(f"No .dispatch.yaml in {repo_path}, skipping")
            continue

        # Scan Linear for this repo's project
        linear_items = await scan_linear(global_config, repo_config)
        all_work.extend(linear_items)
        summary["scanned"] += len(linear_items)

    # Scan Slack (not repo-specific — needs resolution)
    slack_items = await scan_slack(global_config)
    summary["scanned"] += len(slack_items)

    # 3. Filter and dispatch
    for item in all_work:
        # Skip if already in flight
        if state.is_tracked(item.id):
            summary["skipped"] += 1
            continue

        # Dispatch
        result = spawn_agent(item, state)
        if result:
            summary["dispatched"] += 1
            log.info(f"Dispatched {item.id}: {item.title} (PID {result['pid']})")

            # Move to In Progress and comment on the issue
            if item.linear_issue_id:
                creds = item.repo_config.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
                if api_key:
                    await move_to_in_progress(api_key, item.linear_issue_id, item.repo_config.linear_project)
                    await add_comment(api_key, item.linear_issue_id,
                        f"🤖 **Picked up by agent-dispatch.**\n\n"
                        f"Worktree: `{result['worktree']}`\n"
                        f"Branch: `{result['branch']}`")
        else:
            summary["skipped"] += 1
            log.debug(f"Skipped {item.id}: at parallel limit")

    # 4. Cleanup old entries
    state.cleanup_old()

    return summary


def run() -> dict:
    """Synchronous entrypoint for cron."""
    return asyncio.run(run_cycle())
