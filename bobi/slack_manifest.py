"""Generate a Slack app manifest for HTTP Events API or Socket Mode.

The bobi Slack adapter (``event-server/core/src/adapters/chat-sdk-slack.ts``) consumes a
fixed set of events through either transport.
This module renders the bundled HTTP template, optionally transforms it for Socket Mode,
and builds the "create from manifest" deep link.
"""

from __future__ import annotations

import json
import string
from pathlib import Path
from urllib.parse import quote

import yaml

TEMPLATE_PATH = Path(__file__).parent / "templates" / "slack-app.manifest.yaml"

# The single webhook path the event server exposes for Slack (see
# event-server/src/index.ts). The request URL is always <event_server> + this.
WEBHOOK_PATH = "/webhooks/slack"
SOCKET_MODE_HEADER = (
    "# message with thread_ts -> slack.thread_reply. "
    "HTTP Events API, no Socket Mode."
)
SOCKET_MODE_HEADER_REPLACEMENT = (
    "# message with thread_ts -> slack.thread_reply. "
    "Socket Mode, no public Request URL."
)
SOCKET_MODE_FLAG = "  socket_mode_enabled: false"


def webhook_url(event_server: str) -> str:
    """The Slack event-subscriptions request URL for an event server host."""
    return f"{event_server.rstrip('/')}{WEBHOOK_PATH}"


def _replace_manifest_anchor(text: str, anchor: str, replacement: str) -> str:
    """Replace one required manifest anchor or fail on template drift."""
    count = text.count(anchor)
    if count != 1:
        raise ValueError(
            f"Slack manifest anchor must appear exactly once: {anchor!r} "
            f"(found {count})"
        )
    return text.replace(anchor, replacement, 1)


def render_manifest(
    app_name: str,
    event_server: str,
    *,
    socket_mode: bool = False,
) -> str:
    """Render the bundled manifest template as YAML text.

    ``app_name`` becomes the display name.
    ``event_server`` supplies the HTTP request URL and is an inert substitution
    input when ``socket_mode`` removes that URL.
    """
    template = string.Template(TEMPLATE_PATH.read_text())
    rendered = template.safe_substitute(
        APP_NAME=app_name,
        EVENT_SERVER=event_server.rstrip("/"),
    )
    if not socket_mode:
        return rendered

    request_url_line = f"    request_url: {webhook_url(event_server)}"
    rendered = _replace_manifest_anchor(
        rendered, SOCKET_MODE_HEADER, SOCKET_MODE_HEADER_REPLACEMENT,
    )
    rendered = _replace_manifest_anchor(
        rendered, request_url_line + "\n", "",
    )
    return _replace_manifest_anchor(
        rendered, SOCKET_MODE_FLAG, "  socket_mode_enabled: true",
    )


def manifest_to_dict(manifest_yaml: str) -> dict:
    """Parse rendered manifest YAML into a dict."""
    return yaml.safe_load(manifest_yaml)


def manifest_to_json(manifest_yaml: str, *, indent: int | None = 2) -> str:
    """Convert rendered manifest YAML to JSON (Slack accepts either format)."""
    data = manifest_to_dict(manifest_yaml)
    if indent is None:
        return json.dumps(data, separators=(",", ":"))
    return json.dumps(data, indent=indent)


def create_app_url(manifest_yaml: str) -> str:
    """Build the api.slack.com 'create from manifest' deep link.

    Opening it prefills a new-app form with the manifest, so the user only has
    to pick a workspace and click create.
    """
    compact = manifest_to_json(manifest_yaml, indent=None)
    return f"https://api.slack.com/apps?new_app=1&manifest_json={quote(compact)}"
