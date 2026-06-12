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
    from modastack import paths
    agent_yaml = paths.agent_yaml_path(project_path)
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


def monitor_subscription_keys(monitor_events: list[str]) -> list[str]:
    """Topics the manager must subscribe to so monitor findings get delivered.

    The scheduler publishes every monitor finding through
    ``events.publish.post_event(monitor.event, ...)``, which splits the event
    on the first ``/`` and POSTs the *type* to ``/events/<type>`` with the
    source in the body. Current event servers route that onto BOTH the bare
    type (``support.email``) and the source-qualified topic
    (``monitor/support.email``) — see ``createTopicEvent``.

    Both forms are returned anyway: older deployed servers (pre-#235 topic
    contract) deliver only on the bare type, so subscribing to both keeps the
    manager working across server versions. ``deliver()`` dedupes deployments
    across matched topics, so a double subscription never double-delivers.
    """
    keys: list[str] = []
    for event in monitor_events:
        if not event:
            continue
        delivered_topic = event.split("/", 1)[1] if "/" in event else event
        for key in (delivered_topic, event):
            if key not in keys:
                keys.append(key)
    return keys
