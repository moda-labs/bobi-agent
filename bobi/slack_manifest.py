"""Generate a Slack app manifest wired to a bobi event server.

The bobi Slack adapter (``event-server/core/src/adapters/chat-sdk-slack.ts``) consumes a
fixed set of events over the HTTP Events API; the only per-app variables are the
display name and the event server host. This module renders the bundled template
(``templates/slack-app.manifest.yaml``) with those substituted, and builds the
"create from manifest" deep link so a working app can be minted in one click,
via the Slack CLI, or with the App Manifest API.
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


def webhook_url(event_server: str) -> str:
    """The Slack event-subscriptions request URL for an event server host."""
    return f"{event_server.rstrip('/')}{WEBHOOK_PATH}"


def render_manifest(app_name: str, event_server: str) -> str:
    """Render the bundled manifest template as YAML text.

    ``app_name`` becomes the display name; ``event_server`` is the base URL of
    the event server (its ``/webhooks/slack`` path is the request URL).
    """
    template = string.Template(TEMPLATE_PATH.read_text())
    return template.safe_substitute(
        APP_NAME=app_name,
        EVENT_SERVER=event_server.rstrip("/"),
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
