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
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from modastack.config import Config

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
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            return []
        url = result.stdout.strip()
        slug = _parse_github_url(url)
        if slug:
            log.info(f"Auto-detected GitHub repo: {slug}")
            return [f"github:{slug}"]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def _parse_github_url(url: str) -> str:
    """Extract org/repo from a GitHub remote URL."""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com" in url:
        parts = url.split("github.com")[-1].lstrip(":/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return ""


def _is_channel_id(value: str) -> bool:
    """True if the value looks like a Slack channel ID (e.g. C0ABC123)."""
    return bool(value) and value[0] in ("C", "G") and value[1:].isalnum()


def _resolve_channel_names(token: str, channels: list[str]) -> list[str]:
    """Resolve human-readable channel names to Slack channel IDs.

    Accepts a mix of IDs (``C0ABC123``) and names (``#support`` or
    ``support``).  IDs pass through unchanged; names are resolved via
    ``conversations.list``.  Unresolvable names are logged and dropped.
    """
    names_to_resolve: dict[str, int] = {}  # normalized name -> index
    resolved: list[str | None] = list(channels)

    for i, ch in enumerate(channels):
        if _is_channel_id(ch):
            continue
        name = ch.lstrip("#").lower()
        names_to_resolve[name] = i

    if not names_to_resolve:
        return channels

    from modastack import http as pooled

    # Paginate conversations.list to find matching channels.
    cursor = ""
    while True:
        url = "https://slack.com/api/conversations.list?limit=200&types=public_channel,private_channel"
        if cursor:
            url += f"&cursor={cursor}"
        resp = pooled.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
        data = resp.json()

        if not data.get("ok"):
            log.warning(f"conversations.list failed: {data.get('error', 'unknown')}")
            break

        for conv in data.get("channels", []):
            name = conv.get("name", "").lower()
            if name in names_to_resolve:
                idx = names_to_resolve.pop(name)
                resolved[idx] = conv["id"]
                log.info(f"Resolved Slack channel #{name} -> {conv['id']}")
                if not names_to_resolve:
                    break

        if not names_to_resolve:
            break
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    for name in names_to_resolve:
        idx = names_to_resolve[name]
        log.warning(
            f"Could not resolve Slack channel name '{channels[idx]}' to an ID — skipping"
        )
        resolved[idx] = None

    return [ch for ch in resolved if ch is not None]


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
    from modastack import http as pooled

    token = cfg.credential("slack", "bot_token")
    if not token:
        return []
    svc = next((s for s in cfg.services if s.name == "slack"), None)
    channels = svc.channels if svc else []
    try:
        resp = pooled.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        data = resp.json()
        if data.get("ok") and data.get("team_id"):
            if channels:
                channels = _resolve_channel_names(token, channels)
            try:
                from modastack.events.server import _slack_app_id
                app_id = _slack_app_id(token, data.get("bot_id", "") or "")
            except Exception:
                app_id = ""
            keys = _slack_keys(data["team_id"], channels, app_id)
            log.info(
                f"Auto-detected Slack workspace {data['team_id']}; "
                f"subscribing: {keys}"
            )
            return keys
    except Exception as e:
        log.debug(f"Slack auto-detection failed: {e}")
    return []


def _detect_linear(project_path: Path, cfg: "Config") -> list[str]:
    """Detect linear:TEAM from the Linear API."""
    api_key = cfg.credential("linear", "api_key")
    if not api_key:
        return []
    from modastack import http as pooled

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
