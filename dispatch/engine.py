"""Main dispatch engine — the cron entrypoint."""

import asyncio
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import poll_blocked_sessions
from .dispatcher import spawn_agent, check_in_flight, respawn_for_review, read_agent_output, _get_worktree_path
from .pr_monitor import poll_pr_reviews, poll_merged_prs
from .scanner import scan_linear, scan_slack, WorkItem, WorkSource
from .state import StateStore, Status
from .reporter import (
    report_completion, report_failure,
    move_to_in_progress, move_to_in_review, move_to_done, move_to_blocked,
    move_to_planning, move_to_design_review, move_to_implementing,
    add_comment,
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
            tracked = state._items.get(item_id)
            if not tracked:
                continue

            repo_path = Path(tracked.repo_path)
            try:
                repo_config = RepoConfig.from_file(repo_path)
                creds = repo_config.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
            except FileNotFoundError:
                continue

            if tracked.phase == "spec":
                # Spec phase done — post SPEC.md to Linear, enter Design Review
                from .dispatcher import _get_worktree_path
                worktree = _get_worktree_path(tracked)
                spec_content = ""
                if worktree:
                    spec_file = worktree / "SPEC.md"
                    if spec_file.exists():
                        spec_content = spec_file.read_text().strip()

                if api_key and tracked.linear_issue_id:
                    await move_to_design_review(api_key, tracked.linear_issue_id, repo_config.linear_project)

                    # Post full spec across multiple comments if needed
                    chunk_size = 3500
                    if len(spec_content) <= chunk_size:
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Spec ready for review.**\n\n{spec_content}\n\n"
                            f"**Reply 'approved' to proceed with implementation, or provide feedback.**")
                    else:
                        chunks = [spec_content[i:i+chunk_size] for i in range(0, len(spec_content), chunk_size)]
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Spec ready for review.** ({len(chunks)} parts)\n\n{chunks[0]}")
                        for j, chunk in enumerate(chunks[1:], 2):
                            await add_comment(api_key, tracked.linear_issue_id,
                                f"🤖 **Spec (part {j}/{len(chunks)}):**\n\n{chunk}")
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"**Reply 'approved' to proceed with implementation, or provide feedback.**")

                # Enter BLOCKED waiting for approval
                state.update_status(item_id, Status.BLOCKED,
                    pending_question_id=None, phase="spec")
                log.info(f"Spec ready for {item_id} — waiting for design review")

            else:
                # Implementation phase done — move to In Review
                summary["completed"] += 1
                if api_key and tracked.linear_issue_id:
                    await move_to_in_review(api_key, tracked.linear_issue_id, repo_config.linear_project)
                    result = read_agent_output(item_id)
                    summary_text = result.get("output", "")[:1000] if result else ""
                    pr_text = f"\n\nPR: {tracked.pr_url}" if tracked.pr_url else ""
                    comment = f"🤖 **Done.** Ready for review.{pr_text}"
                    if summary_text:
                        comment += f"\n\n**Summary:**\n{summary_text}"
                    await add_comment(api_key, tracked.linear_issue_id, comment)

        elif update["status"] == "blocked":
            # Agent has a question — post it to Linear and move to Blocked
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
                        await move_to_blocked(api_key, tracked.linear_issue_id, repo_config.linear_project)
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
    for unblocked_item in unblocked:
        item_id = unblocked_item["id"]
        reply = unblocked_item.get("reply", "")
        tracked = state._items.get(item_id)
        if not tracked:
            continue

        log.info(f"Unblocked {item_id}: user replied on Linear")

        repo_path = Path(tracked.repo_path)
        try:
            repo_config = RepoConfig.from_file(repo_path)
            creds = repo_config.get_credentials()
            api_key = creds.get("linear_api_key") or global_config.linear_api_key
        except FileNotFoundError:
            continue

        if tracked.phase == "spec":
            # Check if the reply is approval or feedback
            approval_words = ["approved", "approve", "lgtm", "looks good", "go ahead", "ship it", "proceed"]
            is_approved = any(word in reply.lower() for word in approval_words)

            if is_approved:
                # Read the approved spec
                worktree = _get_worktree_path(tracked)
                spec = ""
                if worktree:
                    spec_file = worktree / "SPEC.md"
                    if spec_file.exists():
                        spec = spec_file.read_text()

                # Check if spec recommends splitting into sub-tickets
                from .ticket_splitter import parse_split_from_spec, create_sub_tickets
                split_info = parse_split_from_spec(spec)

                if split_info and split_info.get("split") is True and split_info.get("tickets"):
                    # Create sub-tickets in Linear
                    sub_tickets = await create_sub_tickets(
                        api_key,
                        tracked.linear_issue_id,
                        repo_config.linear_project,
                        split_info["tickets"],
                        repo_config.trigger_labels,
                    )

                    if sub_tickets and api_key:
                        ticket_list = "\n".join(
                            f"- **{t['identifier']}**: {t['title']}" for t in sub_tickets
                        )
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Spec approved. Created {len(sub_tickets)} sub-tickets:**\n\n{ticket_list}\n\n"
                            f"Each sub-ticket will go through its own spec → design review → implement cycle.\n"
                            f"This parent ticket will close when all children are complete.")

                    # Remove parent from state — children will be dispatched individually
                    del state._items[item_id]
                    state._save()
                    log.info(f"Spec approved for {item_id}, created {len(sub_tickets)} sub-tickets")
                else:
                    # Single ticket — spawn implementation phase
                    from .scanner import WorkItem, WorkSource, Complexity
                    work_item = WorkItem(
                        id=item_id,
                        source=WorkSource.LINEAR,
                        title=tracked.title,
                        body="",
                        repo_config=repo_config,
                        complexity=Complexity.MEDIUM,
                        labels=[],
                        linear_issue_id=tracked.linear_issue_id,
                    )

                    # Remove from state so spawn_agent can re-add it
                    del state._items[item_id]
                    state._save()

                    result = spawn_agent(work_item, state, phase="implement", spec=spec)
                    if result and api_key:
                        await move_to_implementing(api_key, tracked.linear_issue_id, repo_config.linear_project)
                        await add_comment(api_key, tracked.linear_issue_id,
                            f"🤖 **Spec approved.** Starting implementation.")
                        log.info(f"Spec approved for {item_id}, starting implementation (PID {result['pid']})")
            else:
                # Feedback — re-spawn spec phase with the feedback
                if api_key:
                    await move_to_planning(api_key, tracked.linear_issue_id, repo_config.linear_project)
                    await add_comment(api_key, tracked.linear_issue_id,
                        f"🤖 **Revising spec based on feedback.**")
                # TODO: re-spawn spec agent with feedback context
                log.info(f"Spec feedback for {item_id}, needs revision")
        else:
            # Implementation phase unblocked
            if api_key:
                await move_to_implementing(api_key, tracked.linear_issue_id, repo_config.linear_project)

    # 1d. Re-dispatch failed items — move back to Todo and clear state (max 3 retries)
    from .reporter import get_team_states, move_issue
    for item_id, item in list(state._items.items()):
        if item.status not in (Status.FAILED, Status.STUCK):
            continue

        if item.attempts >= 3:
            log.warning(f"Giving up on {item_id} after {item.attempts} attempts")
            # Leave in failed state, post to Linear
            if item.linear_issue_id:
                repo_path = Path(item.repo_path)
                try:
                    repo_config = RepoConfig.from_file(repo_path)
                    creds = repo_config.get_credentials()
                    api_key = creds.get("linear_api_key") or global_config.linear_api_key
                    if api_key:
                        await move_to_blocked(api_key, item.linear_issue_id, repo_config.linear_project)
                        await add_comment(api_key, item.linear_issue_id,
                            f"🤖 **Giving up after {item.attempts} attempts.** Needs human intervention.\n\nLast error: {item.error or 'unknown'}")
                except FileNotFoundError:
                    pass
            # Remove from state so we stop retrying
            del state._items[item_id]
            state._save()
            continue

        # Move back to Todo on Linear for retry
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
        # Track attempt count in meta, then remove from state
        meta_path = Path.home() / ".dispatch" / "runs" / item_id
        meta_path.mkdir(parents=True, exist_ok=True)
        attempts_file = meta_path / "attempts"
        attempts_file.write_text(str(item.attempts))

        del state._items[item_id]
        state._save()
        log.info(f"Cleared failed item {item_id} — moved back to Todo for retry (attempt {item.attempts})")

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

        # Dispatch — always starts in spec phase
        result = spawn_agent(item, state, phase="spec")
        if result:
            summary["dispatched"] += 1
            log.info(f"Dispatched {item.id}: {item.title} (spec phase, PID {result['pid']})")

            # Move to Planning and comment on the issue
            if item.linear_issue_id:
                creds = item.repo_config.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
                if api_key:
                    await move_to_planning(api_key, item.linear_issue_id, item.repo_config.linear_project)
                    await add_comment(api_key, item.linear_issue_id,
                        f"🤖 **Picked up by agent-dispatch.** Writing implementation spec.\n\n"
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
