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
from urllib.parse import quote, urlsplit

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

# Wide enough that the YAML emitter never line-wraps a scalar (Slack caps the
# display name at 35 characters; the substitution positions are single-line).
_YAML_WIDTH = 1 << 20


def webhook_url(event_server: str) -> str:
    """The Slack event-subscriptions request URL for an event server host.

    Slack has to reach this host from the internet, so ``event_server`` must be
    an absolute http(s) URL. Anything else — a bare host, a typo'd scheme, a
    value carrying whitespace or a control character — can only produce a
    request URL that quietly doesn't work, so it is refused here, at the one
    place the request URL is built.

    Raises ``ValueError`` for a URL that isn't usable as an event server.
    """
    url = event_server.strip().rstrip("/")
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise ValueError(f"not a usable event server URL: {event_server!r} "
                         f"({e})") from e
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or any(c.isspace() or ord(c) < 0x20 for c in url)):
        raise ValueError(
            "event server URL must be an absolute http(s) URL with no spaces "
            f"(e.g. https://my-worker.workers.dev) — got {event_server!r}")
    return f"{url}{WEBHOOK_PATH}"


def _dump_scalar(value: str, style: str | None) -> str:
    """`value` as a bare YAML scalar in `style` (None = minimal quoting)."""
    dumped = yaml.safe_dump(value, default_flow_style=True, width=_YAML_WIDTH,
                            default_style=style)
    # A plain scalar document ends with an explicit `...` marker; drop it.
    return dumped.removesuffix("\n").removesuffix("\n...")


def _single_line_scalar(value: str, what: str) -> str:
    """Render `value` as a single-line YAML scalar for substitution.

    Every substitution site in the template is a bare scalar position
    (``name:``, ``display_name:``, ``request_url:``), so a user-supplied value
    is otherwise read as YAML: ``Bobi: Staging`` breaks the parse, ``Bobi #1``
    truncates at a comment, ``yes`` / ``2026-07-25`` load as a bool / date, and
    a newline splices in new manifest keys. PyYAML's minimal quoting keeps a
    plain value unquoted (so a simple manifest stays byte-identical to the raw
    template); anything it would spread over several lines is re-emitted
    double-quoted, which escapes the breaks inline. The round-trip assertion is
    the contract: what Slack reads back is exactly what went in.
    """
    scalar = _dump_scalar(value, None)
    if "\n" in scalar:
        scalar = _dump_scalar(value, '"')
    if "\n" in scalar or yaml.safe_load(scalar) != value:  # unreachable guard
        raise ValueError(f"{what} cannot be rendered as YAML: {value!r}")
    return scalar


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

    ``app_name`` becomes the display name and ``event_server`` the request URL.
    Both are validated and escaped as whole YAML scalars, so a value carrying
    YAML syntax renders as data instead of breaking (or silently changing) the
    manifest. The full request URL is substituted in one piece — the template
    must not concatenate a path onto it, or the escaping would end mid-scalar.
    ``event_server`` is still checked when ``socket_mode`` removes that URL: a
    URL Slack could never reach is a mistake worth reporting either way.

    Raises ``ValueError`` for an unusable event server URL, or for an app name
    that cannot be a single-line scalar.
    """
    request_url = _single_line_scalar(webhook_url(event_server),
                                      "event server URL")
    template = string.Template(TEMPLATE_PATH.read_text())
    rendered = template.safe_substitute(
        APP_NAME=_single_line_scalar(app_name, "Slack app name"),
        REQUEST_URL=request_url,
    )
    if not socket_mode:
        return rendered

    rendered = _replace_manifest_anchor(
        rendered, SOCKET_MODE_HEADER, SOCKET_MODE_HEADER_REPLACEMENT,
    )
    rendered = _replace_manifest_anchor(
        rendered, f"    request_url: {request_url}\n", "",
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
