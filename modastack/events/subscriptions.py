"""Auto-discover event subscriptions from the environment."""

import logging
from pathlib import Path

import yaml

from modastack.events.adapters import detect

log = logging.getLogger(__name__)


def discover_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys by auto-detecting event sources.

    Resolution order:
    1. agent.yaml subscribe list (explicit override)
    2. agent.yaml services with events: true (adapters auto-detect keys)
    3. Fallback to project directory name
    """
    agent_yaml = project_path / ".modastack" / "agent.yaml"
    if agent_yaml.exists():
        try:
            raw = yaml.safe_load(agent_yaml.read_text()) or {}
            explicit = raw.get("subscribe", [])
            if explicit:
                return list(explicit)
        except Exception:
            pass

    from modastack.config import Config
    cfg = Config.load(project_path)
    if cfg.event_services:
        subs = []
        for svc in cfg.event_services:
            keys = detect(svc.name, project_path, cfg)
            subs.extend(keys)
        if subs:
            return subs

    return [project_path.name]
