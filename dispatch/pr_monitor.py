"""Monitor open PRs for review feedback and re-dispatch agents."""

import shutil
import subprocess
import json
import logging
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .state import StateStore, Status, TrackedItem

GH_PATH = shutil.which("gh") or "/opt/homebrew/bin/gh"

log = logging.getLogger(__name__)


def get_pr_review_state(repo_path: str, pr_url: str) -> dict | None:
    """Check if a PR has changes requested. Returns review info or None."""
    if not pr_url:
        return None

    # Extract PR number from URL
    try:
        pr_number = pr_url.rstrip("/").split("/")[-1]
    except (IndexError, AttributeError):
        return None

    # Use gh CLI to check review status
    result = subprocess.run(
        [GH_PATH, "pr", "view", pr_number, "--json", "reviewDecision,reviews,comments"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # Check if changes were requested
    if data.get("reviewDecision") == "CHANGES_REQUESTED":
        # Get the review comments
        feedback = []
        for review in data.get("reviews", []):
            if review.get("state") == "CHANGES_REQUESTED":
                body = review.get("body", "").strip()
                if body:
                    feedback.append(body)

        # Also check regular PR comments for feedback
        for comment in data.get("comments", []):
            body = comment.get("body", "").strip()
            if body and not body.startswith("🤖"):  # Skip our own comments
                feedback.append(body)

        return {
            "decision": "CHANGES_REQUESTED",
            "feedback": "\n\n".join(feedback) if feedback else "Changes requested (no specific comments)",
        }

    return None


async def poll_pr_reviews(global_config: GlobalConfig, state: StateStore) -> list[dict]:
    """Check completed items with PRs for review feedback. Returns items to re-dispatch."""
    re_dispatch = []

    for item_id, item in list(state._items.items()):
        if item.status != Status.DONE:
            continue
        if not item.pr_url:
            continue

        review = get_pr_review_state(item.repo_path, item.pr_url)
        if review and review["decision"] == "CHANGES_REQUESTED":
            re_dispatch.append({
                "id": item_id,
                "feedback": review["feedback"],
                "pr_url": item.pr_url,
                "repo_path": item.repo_path,
                "title": item.title,
                "branch": item.branch,
                "linear_issue_id": item.linear_issue_id,
            })

            # Move back to WORKING so it gets re-dispatched
            state.update_status(item_id, Status.WORKING)
            log.info(f"PR changes requested on {item_id}, re-dispatching")

    return re_dispatch


def check_pr_merged(repo_path: str, pr_url: str) -> bool:
    """Check if a PR has been merged."""
    if not pr_url:
        return False

    try:
        pr_number = pr_url.rstrip("/").split("/")[-1]
    except (IndexError, AttributeError):
        return False

    result = subprocess.run(
        [GH_PATH, "pr", "view", pr_number, "--json", "state"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return False

    try:
        data = json.loads(result.stdout)
        return data.get("state") == "MERGED"
    except json.JSONDecodeError:
        return False


async def poll_merged_prs(state: StateStore) -> list[dict]:
    """Check Done items for merged PRs. Returns items to close."""
    merged = []

    for item_id, item in list(state._items.items()):
        if item.status != Status.DONE:
            continue
        if not item.pr_url:
            continue

        if check_pr_merged(item.repo_path, item.pr_url):
            merged.append({
                "id": item_id,
                "pr_url": item.pr_url,
                "linear_issue_id": item.linear_issue_id,
                "repo_path": item.repo_path,
            })

    return merged
