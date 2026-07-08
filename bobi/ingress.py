"""Ingress reachability diagnostics for externally-triggered agents."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_LOCAL_EVENT_SERVER_URL = "http://localhost:8080"


@dataclass(frozen=True)
class IngressWarning:
    """Actionable warning for an inbound event path that cannot reach Bobi."""

    detail: str
    hint: str


def configured_event_server_url(project_path: Path) -> str:
    """Return the configured event server URL, or the local default."""
    from bobi.config import Config

    cfg = Config.load(project_path)
    return cfg.event_server_url or DEFAULT_LOCAL_EVENT_SERVER_URL


def is_unusable_ingress_url(url: str) -> bool:
    """Whether a URL is not public HTTPS ingress webhooks can use."""
    parsed = urlsplit(url)
    if not parsed.hostname and "://" not in url:
        parsed = urlsplit(f"//{url}")
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host:
        return True
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global or addr.is_multicast


def inbound_event_sources(project_path: Path) -> list[str]:
    """Return configured service names that expect inbound webhook delivery."""
    from bobi.config import Config

    cfg = Config.load(project_path)
    return [svc.name for svc in cfg.event_services if svc.name]


def explicit_subscriptions(project_path: Path) -> list[str]:
    """Return explicit manager subscriptions from agent.yaml."""
    import yaml

    from bobi import paths
    from bobi.config import _interpolate_env, project_env

    agent_yaml = paths.agent_yaml_path(project_path)
    if not agent_yaml.exists():
        return []
    raw = yaml.safe_load(agent_yaml.read_text()) or {}
    if not isinstance(raw, dict):
        return []
    value = _interpolate_env(raw.get("subscribe", []), project_env(project_path))
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    return [
        item.strip()
        for item in candidates
        if isinstance(item, str) and item.strip()
    ]


def check_ingress_reachability(
    project_path: Path,
    *,
    extra_subscriptions: list[str] | tuple[str, ...] = (),
) -> IngressWarning | None:
    """Warn when inbound webhooks are configured against loopback ingress.

    Local/inbox-only traffic works with the built-in event server. External
    services such as Slack need a URL they can reach from the public internet.
    """
    sources = inbound_event_sources(project_path) + explicit_subscriptions(project_path)
    sources = [source for source in sources if not source.startswith("inbox/")]
    if not sources and not extra_subscriptions:
        return None

    url = configured_event_server_url(project_path)
    if not is_unusable_ingress_url(url):
        return None

    extras = [source for source in extra_subscriptions if not source.startswith("inbox/")]
    names = sorted(set(sources + extras))
    if not names:
        return None
    label = ", ".join(names)
    return IngressWarning(
        detail=(
            f"inbound events ({label}) are configured but the event server URL "
            f"is {url}, which is not public HTTPS ingress external webhooks can use"
        ),
        hint=(
            "Set event_server_url to the bobi cloud event server, a deployed "
            "Worker, or a public tunnel in front of localhost:8080."
        ),
    )
