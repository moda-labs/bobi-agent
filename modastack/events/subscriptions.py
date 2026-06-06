"""Build subscription keys from project config for event server registration."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def build_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys from project config for event server registration."""
    subs: list[str] = []
    try:
        from modastack.config import ProjectConfig
        pc = ProjectConfig.from_file(project_path)
        if pc.github_repo:
            subs.append(pc.github_repo)
        if pc.slack_workspace_id and pc.slack_channel:
            subs.append(f"slack:{pc.slack_workspace_id}:{pc.slack_channel}")
        elif pc.slack_workspace_id:
            log.warning("slack.workspace_id set but no slack.channel — "
                        "Slack events will not be routed to this agent. "
                        "Set slack.channel in .modastack/config.yaml.")
        if pc.linear_team and pc.task_tracking == "linear":
            subs.append(f"linear:{pc.linear_team}")
    except (FileNotFoundError, Exception) as e:
        log.warning(f"Could not read project config for subscriptions: {e}")
    if not subs:
        subs.append(project_path.name)
    return subs
