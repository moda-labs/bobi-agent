"""Main dispatch engine — simplified.

The engine's job is minimal:
1. Scan Linear for Todo issues with the trigger label
2. Spawn an agent for each (if not already tracked)
3. Check if running agents have exited
4. Re-spawn agents that completed a phase (if user replied on Linear)
5. Clean up done/failed items

The AGENT handles its own Linear state transitions, PR creation, and
commenting — guided by the lifecycle prompt. The engine just watches
processes and detects when to re-spawn.
"""

import asyncio
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import get_latest_human_comment
from .dispatcher import spawn_agent, check_processes, _get_worktree_path
from .scanner import scan_linear, WorkItem, WorkSource
from .state import StateStore, Status

log = logging.getLogger(__name__)


async def run_cycle() -> dict:
    """Run one dispatch cycle."""
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"scanned": 0, "dispatched": 0, "completed": 0, "failed": 0, "skipped": 0, "unblocked": 0}

    # 1. Check running processes
    updates = check_processes(state)
    for update in updates:
        if update["status"] == "done":
            summary["completed"] += 1
            log.info(f"Completed {update['id']}")
        elif update["status"] == "failed":
            summary["failed"] += 1
            log.info(f"Failed {update['id']}")

    # 2. Check done items for user replies (triggers re-spawn for next phase)
    for item_id, item in list(state._items.items()):
        if item.status != Status.DONE:
            continue
        if not item.linear_issue_id:
            continue

        # Resolve credentials
        try:
            repo_config = RepoConfig.from_file(Path(item.repo_path))
            creds = repo_config.get_credentials()
            api_key = creds.get("linear_api_key") or global_config.linear_api_key
        except FileNotFoundError:
            continue

        if not api_key:
            continue

        # Check for a human reply since the agent last ran
        reply = await get_latest_human_comment(api_key, item.linear_issue_id)
        if not reply:
            continue

        # Don't re-spawn if the reply is old (already acted on)
        if item.last_reply == reply:
            continue

        # Re-spawn with the reply as context
        log.info(f"Re-spawning {item_id}: user replied")

        # Build a WorkItem for re-spawning
        work_item = WorkItem(
            id=item_id,
            source=WorkSource.LINEAR,
            title=item.title,
            body="",
            repo_config=repo_config,
            labels=[],
            linear_issue_id=item.linear_issue_id,
        )

        # Remove from state so spawn_agent can re-add
        del state._items[item_id]
        state._save()

        result = spawn_agent(work_item, state, user_reply=reply)
        if result:
            summary["unblocked"] += 1
            log.info(f"Re-dispatched {item_id} (PID {result['pid']})")

    # 3. Clear failed items (max 3 retries)
    for item_id, item in list(state._items.items()):
        if item.status != Status.FAILED:
            continue
        if item.attempts >= 3:
            log.warning(f"Giving up on {item_id} after {item.attempts} attempts")
            del state._items[item_id]
            state._save()
            continue
        # Clear for retry — scanner will pick it up again
        meta_path = Path.home() / ".dispatch" / "runs" / item_id
        meta_path.mkdir(parents=True, exist_ok=True)
        (meta_path / "attempts").write_text(str(item.attempts))
        del state._items[item_id]
        state._save()
        log.info(f"Cleared {item_id} for retry (attempt {item.attempts})")

    # 4. Scan for new work
    for repo_path in global_config.repos:
        if not repo_path.exists():
            continue

        try:
            repo_config = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            continue

        linear_items = await scan_linear(global_config, repo_config)
        summary["scanned"] += len(linear_items)

        for item in linear_items:
            if state.is_tracked(item.id):
                summary["skipped"] += 1
                continue

            result = spawn_agent(item, state)
            if result:
                summary["dispatched"] += 1
                log.info(f"Dispatched {item.id}: {item.title} (PID {result['pid']})")
            else:
                summary["skipped"] += 1

    # 5. Cleanup
    state.cleanup_old()

    return summary


def run() -> dict:
    """Synchronous entrypoint."""
    return asyncio.run(run_cycle())
