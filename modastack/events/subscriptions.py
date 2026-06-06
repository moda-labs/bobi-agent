"""Build subscription keys from agent.yaml for event server registration."""

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def build_subscriptions(project_path: Path) -> list[str]:
    """Read subscribe list from .modastack/agent.yaml."""
    agent_yaml = project_path / ".modastack" / "agent.yaml"
    if agent_yaml.exists():
        try:
            raw = yaml.safe_load(agent_yaml.read_text()) or {}
            subs = raw.get("subscribe", [])
            if subs:
                return list(subs)
        except Exception as e:
            log.warning(f"Could not read agent.yaml for subscriptions: {e}")
    return [project_path.name]
