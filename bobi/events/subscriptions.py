"""Auto-discover event subscriptions from the environment."""

import logging
from pathlib import Path

import yaml

from bobi.events.adapters import detect

log = logging.getLogger(__name__)


def _normalize_explicit_subscriptions(value) -> list[str]:
    """Return non-empty string subscription keys from a YAML subscribe value."""
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    return [item.strip() for item in candidates
            if isinstance(item, str) and item.strip()]


def explicit_subscriptions(project_path: Path) -> list[str]:
    """Return agent.yaml's explicit `subscribe:` keys, env-interpolated.

    The ONE parser for the `subscribe:` surface (D078). `discover_subscriptions`
    reads it as its highest-precedence source and `bobi.ingress` reads it to
    decide which topics a reachability warning must cover; when each had its own
    copy, a schema change could make the warning disagree with what the session
    actually subscribes to.

    Errors are NOT swallowed here — the two callers want different things from a
    malformed agent.yaml, so each decides for itself.
    """
    from bobi import paths
    from bobi.config import _interpolate_env, project_env

    agent_yaml = paths.agent_yaml_path(project_path)
    if not agent_yaml.exists():
        return []
    raw = yaml.safe_load(agent_yaml.read_text()) or {}
    if not isinstance(raw, dict):
        return []
    return _normalize_explicit_subscriptions(
        _interpolate_env(raw.get("subscribe", []), project_env(project_path))
    )


def discover_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys by auto-detecting event sources.

    Resolution order:
    1. agent.yaml subscribe list (explicit override)
    2. agent.yaml services with events: true (adapters auto-detect keys)
    3. Fallback to project directory name
    """
    try:
        explicit = explicit_subscriptions(project_path)
        if explicit:
            return explicit
    except Exception:
        # An unreadable agent.yaml falls through to auto-detection rather than
        # failing the whole discovery — the pre-consolidation behavior here.
        pass

    from bobi.config import Config
    cfg = Config.load(project_path)
    if cfg.event_services:
        subs = []
        for svc in cfg.event_services:
            keys = detect(svc.name, project_path, cfg)
            subs.extend(keys)
        if subs:
            return subs

    return [project_path.name]


# Sub-agent lifecycle topics the persistent entry point must hear so a detached
# agent's completion/failure is delivered back to the launcher instead of being
# emitted into the void (MDS-65 RC#1). _emit_session_finished already POSTs these
# carrying requested_by; nothing subscribed to them before.
LIFECYCLE_EVENTS = ("agent/session.completed", "agent/session.failed")


def lifecycle_subscription_keys() -> list[str]:
    """Topics the entry point subscribes to so sub-agent completions reach it.

    Mirrors ``monitor_subscription_keys``: returns BOTH the bare delivered type
    (``session.completed``) and the source-qualified topic
    (``agent/session.completed``). Current servers route a posted event onto both
    forms; older servers (pre-#235 topic contract) deliver only the bare type, so
    subscribing to both keeps delivery working across server versions.
    ``deliver()`` dedupes across matched topics, so the double subscription never
    double-delivers.
    """
    keys: list[str] = []
    for event in LIFECYCLE_EVENTS:
        delivered_topic = event.split("/", 1)[1] if "/" in event else event
        for key in (delivered_topic, event):
            if key not in keys:
                keys.append(key)
    return keys


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
