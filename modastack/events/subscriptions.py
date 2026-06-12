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


def monitor_subscription_keys(monitor_events: list[str]) -> list[str]:
    """Topics the manager must subscribe to so monitor findings get delivered.

    A description-only monitor posts its finding through
    ``events.publish.post_event(monitor.event, ...)``, which splits the event
    on the first ``/`` and POSTs only the *type* to ``/events/<type>`` (the
    ``monitor`` source goes into the body). The event server's
    ``createTopicEvent`` then routes that POST onto the **path topic** — the
    bare type, e.g. ``support.email`` — because the body carries no
    repo/team/workspace routing field.

    So the topic a finding is *delivered* on is the type, NOT the full
    ``monitor/support.email`` string. Subscribing to the raw event string
    (as the manager did before) never matches, and the finding is silently
    dropped with ``delivered_to: 0``. Subscribe to the delivered topic.

    The raw event string is also kept for defensiveness/back-compat — it is
    harmless if nothing is ever delivered on it.
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
