"""Build subscription keys from project config for event server registration."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def build_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys from project config.

    Derives keys from config fields (slack.workspace_id, linear.team).
    GitHub subscriptions come from agent.yaml's subscribe list directly.
    """
    subs: list[str] = []
    try:
        from modastack.config import Config
        cfg = Config.load(project_path)
        if cfg.slack_workspace_id:
            subs.append(f"slack:{cfg.slack_workspace_id}")
        if cfg.linear_team:
            subs.append(f"linear:{cfg.linear_team}")
    except Exception as e:
        log.warning(f"Could not read project config for subscriptions: {e}")
    if not subs:
        subs.append(project_path.name)
    return subs
