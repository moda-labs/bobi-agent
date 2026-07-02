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


def is_loopback_url(url: str) -> bool:
    """Whether a URL points at local/private ingress webhooks cannot reach."""
    parsed = urlsplit(url)
    if not parsed.hostname and "://" not in url:
        parsed = urlsplit(f"//{url}")
    host = parsed.hostname or ""
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_unspecified
        or addr.is_private
        or addr.is_link_local
    )


def inbound_event_sources(project_path: Path) -> list[str]:
    """Return configured service names that expect inbound webhook delivery."""
    from bobi.config import Config

    cfg = Config.load(project_path)
    return [svc.name for svc in cfg.event_services if svc.name]


def check_ingress_reachability(
    project_path: Path,
    *,
    extra_subscriptions: list[str] | tuple[str, ...] = (),
) -> IngressWarning | None:
    """Warn when inbound webhooks are configured against loopback ingress.

    Local/inbox-only traffic works with the built-in event server. External
    services such as Slack need a URL they can reach from the public internet.
    """
    sources = inbound_event_sources(project_path)
    if not sources and not extra_subscriptions:
        return None

    url = configured_event_server_url(project_path)
    if not is_loopback_url(url):
        return None

    names = sorted(set(sources + list(extra_subscriptions)))
    label = ", ".join(names)
    return IngressWarning(
        detail=(
            f"inbound events ({label}) are configured but the event server URL "
            f"is {url}, which external webhooks cannot reach"
        ),
        hint=(
            "Set event_server_url to the bobi cloud event server, a deployed "
            "Worker, or a public tunnel in front of localhost:8080."
        ),
    )
