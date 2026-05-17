"""Main dispatch engine — the cron entrypoint."""

import asyncio
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .scanner import scan_linear, scan_slack, WorkItem, WorkSource
from .state import StateStore, Status
from .dispatcher import spawn_agent, check_in_flight
from .reporter import report_completion, report_failure

log = logging.getLogger(__name__)


async def run_cycle() -> dict:
    """Run one dispatch cycle. Called by cron every N minutes.

    Returns a summary dict of what happened this cycle.
    """
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"scanned": 0, "dispatched": 0, "completed": 0, "failed": 0, "skipped": 0}

    # 1. Check in-flight work first
    updates = await check_in_flight(state)
    for update in updates:
        if update["status"] == "auditing":
            summary["completed"] += 1

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
        pid = spawn_agent(item, state)
        if pid:
            summary["dispatched"] += 1
            log.info(f"Dispatched {item.id}: {item.title} (PID {pid})")
        else:
            summary["skipped"] += 1
            log.debug(f"Skipped {item.id}: at parallel limit")

    # 4. Report completed/failed items
    for item in state.get_in_flight():
        if item.status == Status.DONE:
            repo_path = Path(item.repo_path)
            try:
                repo_config = RepoConfig.from_file(repo_path)
                await report_completion(global_config, repo_config, item)
            except FileNotFoundError:
                pass

        elif item.status in (Status.FAILED, Status.STUCK):
            summary["failed"] += 1
            repo_path = Path(item.repo_path)
            try:
                repo_config = RepoConfig.from_file(repo_path)
                await report_failure(global_config, repo_config, item)
            except FileNotFoundError:
                pass

    # 5. Cleanup old entries
    state.cleanup_old()

    return summary


def run() -> dict:
    """Synchronous entrypoint for cron."""
    return asyncio.run(run_cycle())
