"""Ingestion adapter registry for event sources.

Each adapter provides a detect() function that auto-discovers subscription
keys for a service from the project environment. The registry replaces the
hardcoded if/elif chain in subscriptions.py and the hardcoded
native_services list in config.py.

Framework-shipped adapters (github, slack, linear) register here. Adding a
new native event source means adding one adapter module — zero core edits.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from bobi.config import Config

log = logging.getLogger(__name__)


class Detector(Protocol):
    """A callable that discovers subscription keys for a service."""

    def __call__(self, project_path: Path, cfg: "Config") -> list[str]: ...


# --- Registry ---

_registry: dict[str, Detector] = {}


def register(name: str, detector: Detector) -> None:
    """Register an ingestion adapter's subscription detector."""
    _registry[name] = detector


def is_registered(name: str) -> bool:
    """True if a native ingestion adapter exists for this service name."""
    return name in _registry


def detect(name: str, project_path: Path, cfg: "Config") -> list[str]:
    """Run the detector for a service, falling back to [name] if unregistered."""
    detector = _registry.get(name)
    if detector is None:
        return [name]
    return detector(project_path, cfg)


# --- Built-in adapters ---


def _detect_github(project_path: Path, cfg: "Config") -> list[str]:
    """Detect github:org/repo from git remote.

    If the project root is not itself a git repo (director-style
    deployments run from a parent directory of repos), detect the
    remote of each immediate child repo instead.
    """
    keys = _github_remote_key(project_path)
    if keys:
        return keys
    if (project_path / ".git").exists():
        return []
    subs: list[str] = []
    try:
        children = sorted(p for p in project_path.iterdir() if p.is_dir())
    except OSError:
        return []
    for child in children:
        if (child / ".git").exists():
            subs.extend(_github_remote_key(child))
    return subs


def _github_remote_key(repo_path: Path) -> list[str]:
    """Subscription key for a single repo's GitHub origin remote."""
    from bobi.gitutil import origin_url

    slug = _parse_github_url(origin_url(repo_path))
    if slug:
        log.info(f"Auto-detected GitHub repo: {slug}")
        return [f"github:{slug}"]
    return []


def _parse_github_url(url: str) -> str:
    """Extract org/repo from a github.com remote URL, else "".

    The host is compared exactly, not searched for (D075). A substring test
    made every GitHub Enterprise host — ``github.company.com`` — look like
    github.com followed by a path, producing a garbage slug ('pany.com/acme')
    and a subscription topic no event can ever match; it also accepted
    lookalikes such as ``notgithub.com`` and ``github.com.evil.test``. bobi
    talks only to github.com here, so anything else yields no slug.
    """
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    rest = url.split("://", 1)[1] if "://" in url else url
    # Strip any userinfo (git@, user:token@) ahead of the host.
    head = rest.split("/", 1)[0]
    if "@" in head:
        rest = rest.split("@", 1)[1]

    # scp-style `host:org/repo` vs URL-style `host/org/repo`. A numeric
    # segment after the colon is a port, not the path.
    head = rest.split("/", 1)[0]
    if ":" in head:
        host, _, after = rest.partition(":")
        first = after.split("/", 1)[0]
        path = after.split("/", 1)[1] if first.isdigit() else after
    else:
        host, _, path = rest.partition("/")

    if host.lower() != "github.com":
        return ""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _resolve_channel_names(token: str, channels: list[str]) -> list[str]:
    """Resolve configured channel references to Slack channel IDs.

    Accepts a mix of IDs (``C0ABC123``), names (``#support`` or ``support``)
    and ``@handle`` DMs. The ID-vs-name decision and the lookup itself belong
    to :func:`bobi.slack.resolve_channel_id`, the house's single code path for
    them; this only adds the subscription-side policy that a reference which
    cannot be resolved is logged and dropped rather than raised, so one bad
    channel never costs the agent every other subscription.

    Only ``RuntimeError`` — the "no such channel" / "Slack said not ok" signal
    — is absorbed. A transport failure still propagates, because the caller
    turns that into "no Slack subscription at all"; swallowing it here would
    silently promote a channel-scoped subscription to a workspace-wide one.
    """
    from bobi.slack import resolve_channel_id

    resolved: list[str] = []
    for ch in channels:
        try:
            channel_id = resolve_channel_id(token, ch)
        except RuntimeError as e:
            log.warning(
                f"Could not resolve Slack channel name '{ch}' to an ID — skipping ({e})"
            )
            continue
        if channel_id:
            resolved.append(channel_id)
    return resolved


def _slack_keys(team_id: str, channels: list[str],
                app_id: str = "") -> list[str]:
    """Build Slack subscription keys for a workspace.

    When the app id is known, subscribe app-qualified so multiple Slack apps in
    one workspace do not receive each other's DMs. With channels configured,
    also scope to the channel.
    """
    if not team_id:
        return []
    prefix = f"slack:{team_id}:app:{app_id}" if app_id else f"slack:{team_id}"
    if channels:
        return [f"{prefix}:{ch}" for ch in channels]
    return [prefix]


def _detect_slack(project_path: Path, cfg: "Config") -> list[str]:
    """Detect Slack subscription keys from the bot token via auth.test.

    Scopes to the slack service's configured `channels` if any are set.
    Channels can be IDs (``C0ABC123``) or human-readable names
    (``#support`` or ``support``) — names are resolved to IDs
    automatically via the Slack API.
    """
    from bobi.slack import SlackAppIdentityError, require_app_identity

    token = cfg.credential("slack", "bot_token")
    if not token:
        return []
    svc = next((s for s in cfg.services if s.name == "slack"), None)
    channels = svc.channels if svc else []
    try:
        team_id, _bot_id, _bot_user_id, app_id = require_app_identity(token)
        if channels:
            channels = _resolve_channel_names(token, channels)
        keys = _slack_keys(team_id, channels, app_id)
        log.info(
            f"Auto-detected Slack workspace {team_id}; "
            f"subscribing: {keys}"
        )
        return keys
    except SlackAppIdentityError as e:
        log.warning("Slack event subscription disabled: %s", e)
    except Exception as e:
        log.debug(f"Slack auto-detection failed: {e}")
    return []


def _detect_whatsapp(project_path: Path, cfg: "Config") -> list[str]:
    """Detect the WhatsApp subscription key from the configured number (#656).

    Validates the credential upstream before subscribing, same contract as
    ``_detect_slack``'s auth.test: the ``whatsapp:<pnid>`` grant is written by
    the signed number registration, so subscribing with a credential the
    registration will reject would hard-fail the WHOLE deployment
    registration (#488 rejects unauthorized global topics atomically) and
    take every other subscription down with it.
    """
    import os

    pnid = cfg.credential("whatsapp", "phone_number_id")
    token = cfg.credential("whatsapp", "access_token")
    if not (pnid and token):
        return []
    from bobi import http as pooled

    graph = os.environ.get(
        "BOBI_ES_WHATSAPP_API_URL", "https://graph.facebook.com/v23.0/")
    if not graph.endswith("/"):
        graph += "/"
    try:
        resp = pooled.get(
            f"{graph}{pnid}?fields=id",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if str(resp.json().get("id", "")) != pnid:
            log.warning(
                "WhatsApp credential rejected by the Graph API for number %s "
                "- not subscribing (check WHATSAPP_ACCESS_TOKEN / "
                "WHATSAPP_PHONE_NUMBER_ID)", pnid)
            return []
    except Exception as e:
        log.warning("WhatsApp auto-detection failed for %s: %s", pnid, e)
        return []
    key = f"whatsapp:{pnid}"
    log.info(f"Auto-detected WhatsApp number {pnid}; subscribing: [{key!r}]")
    return [key]


def _detect_discord(project_path: Path, cfg: "Config") -> list[str]:
    """Detect the Discord subscription key from the configured app (#2).

    Validates the credential upstream before subscribing, same contract as
    ``_detect_whatsapp``: the ``discord:<application_id>`` grant is written by
    the signed app registration, so subscribing with a credential the
    registration will reject would hard-fail the WHOLE deployment
    registration (#488 rejects unauthorized global topics atomically).
    """
    import os

    app_id = cfg.credential("discord", "application_id")
    token = cfg.credential("discord", "bot_token")
    if not (app_id and token):
        return []
    from bobi import http as pooled

    api = os.environ.get("BOBI_ES_DISCORD_API_URL", "https://discord.com/api/v10/")
    if not api.endswith("/"):
        api += "/"
    try:
        resp = pooled.get(
            f"{api}applications/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=5.0,
        )
        if str(resp.json().get("id", "")) != app_id:
            log.warning(
                "Discord credential rejected by the API for application %s "
                "- not subscribing (check DISCORD_BOT_TOKEN / "
                "DISCORD_APPLICATION_ID)", app_id)
            return []
    except Exception as e:
        log.warning("Discord auto-detection failed for %s: %s", app_id, e)
        return []
    key = f"discord:{app_id}"
    log.info(f"Auto-detected Discord app {app_id}; subscribing: [{key!r}]")
    return [key]


def _detect_linear(project_path: Path, cfg: "Config") -> list[str]:
    """Detect linear:TEAM from the Linear API."""
    api_key = cfg.credential("linear", "api_key")
    if not api_key:
        return []
    from bobi import http as pooled

    try:
        resp = pooled.post(
            "https://api.linear.app/graphql",
            json={"query": "{ teams { nodes { key } } }"},
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            timeout=5.0,
        )
        data = resp.json()
        teams = data.get("data", {}).get("teams", {}).get("nodes", [])
        keys = [f"linear:{t['key']}" for t in teams if t.get("key")]
        if keys:
            log.info(f"Auto-detected Linear teams: {keys}")
        return keys
    except Exception as e:
        log.debug(f"Linear auto-detection failed: {e}")
    return []


# Register built-in adapters at import time.
register("github", _detect_github)
register("slack", _detect_slack)
register("linear", _detect_linear)
register("whatsapp", _detect_whatsapp)
register("discord", _detect_discord)
