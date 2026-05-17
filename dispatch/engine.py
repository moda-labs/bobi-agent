"""Main dispatch engine — the cron entrypoint."""

import asyncio
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import poll_blocked_sessions
from .dispatcher import spawn_agent, check_in_flight, respawn_for_review, read_agent_output
from .pr_monitor import poll_pr_reviews, poll_merged_prs
from .scanner import scan_linear, scan_slack, WorkItem, WorkSource
from .state import StateStore, Status
from .reporter import (
    report_completion, report_failure,
    move_to_in_progress, move_to_in_review, move_to_done, add_comment,
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

        if update["status"] == "progress":
            tracked = state._items.get(item_id)
            if tracked and tracked.linear_issue_id:
                repo_path = Path(tracked.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Progress update:**\n\n{update['progress']}")
                except FileNotFoundError:
                    pass

        elif update["status"] == "done":
            summary["completed"] += 1
            tracked = state._items.get(item_id)
            if tracked and tracked.linear_issue_id:
                repo_path = Path(tracked.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        await move_to_in_review(api_key, tracked.linear_issue_id, repo_config.linear_project)
                        # Post summary of what was done
                        result = read_agent_output(item_id)
                        summary_text = result.get("output", "")[:1000] if result else ""
                        pr_text = f"\n\nPR: {tracked.pr_url}" if tracked.pr_url else ""
                        comment = f"🤖 **Done.** Ready for review.{pr_text}"
                        if summary_text:
                            comment += f"\n\n**Summary:**\n{summary_text}"
                        await add_comment(api_key, tracked.linear_issue_id, comment)
                except FileNotFoundError:
                    pass

        elif update["status"] == "blocked":
            # Agent has a question — post it to Linear
            tracked = state._items.get(item_id)
            if tracked and tracked.linear_issue_id:
                repo_path = Path(tracked.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        from .conversation import post_question
                        question_text = update.get("question", "Agent needs input")
                        comment_id = await post_question(api_key, tracked.linear_issue_id, question_text)
                        if comment_id:
                            state.update_status(item_id, Status.BLOCKED,
                                pending_question_id=comment_id)
                        log.info(f"Blocked {item_id}: question posted to Linear")
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

    # 1b. Poll PRs for review feedback — re-dispatch if changes requested
    pr_reviews = await poll_pr_reviews(global_config, state)
    for review_item in pr_reviews:
        item_id = review_item["id"]
        tracked = state._items.get(item_id)
        if not tracked or not tracked.linear_issue_id:
            continue

        repo_path = Path(tracked.repo_path)
        try:
            repo_config = RepoConfig.from_file(repo_path)
            creds = repo_config.get_credentials()
            api_key = creds.get("linear_api_key") or global_config.linear_api_key
        except FileNotFoundError:
            continue

        # Post to Linear that we're addressing feedback
        if api_key:
            await move_to_in_progress(api_key, tracked.linear_issue_id, repo_config.linear_project)
            await add_comment(api_key, tracked.linear_issue_id,
                f"🤖 **Addressing PR feedback.**\n\n{review_item['feedback'][:500]}")

        # Re-spawn in the same worktree
        result = respawn_for_review(item_id, review_item["feedback"], state)
        if result:
            log.info(f"Re-dispatched {item_id} for PR review feedback (PID {result['pid']})")

    # 1c. Poll blocked sessions for user replies on Linear
    unblocked = await poll_blocked_sessions(global_config, state)
    summary["unblocked"] = len(unblocked)
    for item in unblocked:
        log.info(f"Unblocked {item['id']}: user replied on Linear")

    # 1d. Re-dispatch failed items — move back to Todo and clear state
    from .reporter import get_team_states, move_issue
    for item_id, item in list(state._items.items()):
        if item.status not in (Status.FAILED, Status.STUCK):
            continue
        # Move back to Todo on Linear
        if item.linear_issue_id:
            repo_path = Path(item.repo_path)
            try:
                repo_config = RepoConfig.from_file(repo_path)
                creds = repo_config.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
                if api_key:
                    states = await get_team_states(api_key, repo_config.linear_project)
                    todo_id = states.get("todo") or states.get("unstarted")
                    if todo_id:
                        await move_issue(api_key, item.linear_issue_id, todo_id)
            except FileNotFoundError:
                pass
        # Remove from state so the scanner picks it up fresh
        del state._items[item_id]
        state._save()
        log.info(f"Cleared failed item {item_id} — moved back to Todo for retry")

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

    # 4. Check for merged PRs — close the Linear issue
    merged = await poll_merged_prs(state)
    for merged_item in merged:
        item_id = merged_item["id"]
        linear_issue_id = merged_item.get("linear_issue_id")
        if linear_issue_id:
            repo_path = Path(merged_item["repo_path"])
            try:
                repo_config = RepoConfig.from_file(repo_path)
                creds = repo_config.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
                if api_key:
                    await move_to_done(api_key, linear_issue_id, repo_config.linear_project)
                    await add_comment(api_key, linear_issue_id,
                        f"🤖 **PR merged.** Issue complete.")
            except FileNotFoundError:
                pass

        # Remove from state — fully done
        if item_id in state._items:
            del state._items[item_id]
            state._save()
        log.info(f"Closed {item_id}: PR merged")

    # 5. Cleanup old entries
    state.cleanup_old()

    return summary


def run() -> dict:
    """Synchronous entrypoint for cron."""
    return asyncio.run(run_cycle())
