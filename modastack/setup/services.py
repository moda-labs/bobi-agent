"""Connector catalog for the Connect stage.

Connect is the deliberate exception to the magic: credential/access grants
must be *visible and approved*, so each implied service becomes a card that
states plainly what the team will be able to reach (granted scopes), how to
authorize it (a native token or a Venn OAuth connection), and whether it's
currently connected.

Two kinds of connector:
  - **native** — the framework ships an ingestion adapter (github / slack /
    linear). Reached with a direct token in `.env`; events arrive by webhook.
  - **venn** — everything else. Reached through the Venn gateway with the
    shared `VENN_API_KEY`; the user authorizes the underlying service via
    Venn's OAuth, and connection is verified against Venn's connected servers.

This module is the *catalog + pure status logic*. Live Venn lookups and
`.env` reads are thin wrappers around it (the web server calls them once per
Connect render); the pure `card()` is what the tests pin down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from modastack.venn import SERVICE_ALIASES

VENN_KEY_VAR = "VENN_API_KEY"


@dataclass(frozen=True)
class Connector:
    key: str                       # canonical id (matches a spec service name)
    name: str                      # display name
    kind: str                      # "native" | "venn"
    summary: str                   # what the team does with it
    scopes: tuple = ()             # human-readable granted capabilities
    credential_var: str = ""       # env var to collect (native); "" for venn
    instructions: str = ""         # where to find the token (native)
    aliases: tuple = ()            # alternate names that resolve here

    def names(self) -> set:
        return {self.key, *self.aliases}


# Native connectors — the framework has an ingestion adapter for each.
_NATIVE = [
    Connector(
        key="github", name="GitHub", kind="native",
        summary="Read and act on issues, pull requests, and reviews.",
        scopes=("read/write issues & PRs", "post comments", "receive webhooks"),
        credential_var="GITHUB_TOKEN",
        instructions="A GitHub personal access token (repo scope), or leave "
                     "blank to use the `gh` CLI / git remote already on this "
                     "machine.",
    ),
    Connector(
        key="slack", name="Slack", kind="native",
        summary="Send and receive messages; reply in threads.",
        scopes=("post messages", "read channels the bot is in",
                "receive events"),
        credential_var="SLACK_BOT_TOKEN",
        instructions="Slack bot token (starts with xoxb-) from your app's "
                     "OAuth & Permissions page.",
        aliases=("slackbot",),
    ),
    Connector(
        key="linear", name="Linear", kind="native",
        summary="Read and update issues, projects, and cycles.",
        scopes=("read/write issues", "receive webhooks"),
        credential_var="LINEAR_API_KEY",
        instructions="Linear API key from Settings → API → Personal API keys.",
    ),
]

# Venn-backed connectors — reached through the Venn gateway. Keys mirror the
# coarse service buckets in venn.SERVICE_ALIASES so a spec that says "email"
# or "crm" resolves cleanly; aliases pull in the concrete product names.
_VENN = [
    Connector(key="email", name="Email", kind="venn",
              summary="Read and send mail.",
              scopes=("read messages", "send messages"),
              aliases=tuple(SERVICE_ALIASES["email"])),
    Connector(key="calendar", name="Calendar", kind="venn",
              summary="Read and create events.",
              scopes=("read events", "create events"),
              aliases=tuple(SERVICE_ALIASES["calendar"])),
    Connector(key="docs", name="Docs", kind="venn",
              summary="Read and edit documents.",
              scopes=("read docs", "edit docs"),
              aliases=tuple(SERVICE_ALIASES["docs"])),
    Connector(key="sheets", name="Sheets", kind="venn",
              summary="Read and write spreadsheets.",
              scopes=("read sheets", "write sheets"),
              aliases=tuple(SERVICE_ALIASES["sheets"])),
    Connector(key="storage", name="File storage", kind="venn",
              summary="Read and write files in cloud storage.",
              scopes=("list files", "read/write files"),
              aliases=tuple(SERVICE_ALIASES["storage"])),
    Connector(key="crm", name="CRM", kind="venn",
              summary="Read and update CRM records.",
              scopes=("read records", "update records"),
              aliases=tuple(SERVICE_ALIASES["crm"])),
    Connector(key="tickets", name="Issue tracker", kind="venn",
              summary="Read and update tickets.",
              scopes=("read tickets", "update tickets"),
              aliases=tuple(SERVICE_ALIASES["tickets"])),
]

CATALOG: dict[str, Connector] = {c.key: c for c in (*_NATIVE, *_VENN)}

# Reverse index: every name/alias → canonical key. Aliases first, then
# canonical keys, so a real connector key always wins over another
# connector's alias (e.g. native "linear" beats the venn "tickets" alias).
_BY_NAME: dict[str, str] = {}
for _c in CATALOG.values():
    for _alias in _c.aliases:
        _BY_NAME.setdefault(_alias.lower(), _c.key)
for _c in CATALOG.values():
    _BY_NAME[_c.key.lower()] = _c.key


def resolve(name: str) -> Connector:
    """Resolve a (possibly messy) service name to a Connector.

    Unknown names become a generic Venn connector — the gateway may still
    have it even when the curated catalog doesn't.
    """
    key = _BY_NAME.get(name.strip().lower())
    if key:
        return CATALOG[key]
    clean = name.strip()
    return Connector(
        key=clean.lower(), name=clean or "service", kind="venn",
        summary="Reached through the Venn gateway.",
        scopes=("as granted in Venn",),
    )


def _status(connector: Connector, *, has_credential: bool,
            venn_connected: bool | None) -> str:
    """connected | missing | unknown — the pure card-status rule."""
    if connector.kind == "venn":
        if venn_connected is None:
            return "unknown"
        return "connected" if venn_connected else "missing"
    # native
    if connector.credential_var:
        return "connected" if has_credential else "missing"
    return "connected"   # nothing to collect (e.g. github via gh CLI)


def card(connector: Connector, *, has_credential: bool = False,
         venn_connected: bool | None = None) -> dict:
    """A serializable Connect card for one connector, with its status."""
    return {
        "key": connector.key,
        "name": connector.name,
        "kind": connector.kind,
        "summary": connector.summary,
        "scopes": list(connector.scopes),
        "credential_var": connector.credential_var,
        "instructions": connector.instructions,
        "via": "Venn OAuth" if connector.kind == "venn" else "token",
        "status": _status(connector, has_credential=has_credential,
                          venn_connected=venn_connected),
    }


def catalog_cards() -> list[dict]:
    """Every known connector as a status-less card — the 'add what was
    missed' picker for Connect."""
    return [card(c) for c in CATALOG.values()]


def venn_connected_names(project: Path, key: str | None = None) -> set[str] | None:
    """Lowercased names of services currently connected in the user's Venn
    account, or None when no key is available / Venn can't be reached.

    Thin live wrapper — the pure status logic lives in `card()`.
    """
    from modastack.setup.actions import venn_key
    from modastack.venn import list_servers

    key = key or venn_key(project)
    if not key:
        return None
    try:
        servers = list_servers(key)
    except Exception:
        return None
    return {s.server_name.lower() for s in servers if s.connected}


def cards_for(service_names, project: Path,
              connected: set | None = None) -> list[dict]:
    """Build Connect cards for the services the spec implies, deduped by
    connector. `connected` is a precomputed set of connected Venn server
    names (from `venn_connected_names`); None means "not checked".
    """
    from modastack.setup.actions import read_env

    env = read_env(project)
    seen: set[str] = set()
    cards: list[dict] = []
    for raw in service_names:
        name = raw.get("name", "") if isinstance(raw, dict) else str(raw)
        if not name:
            continue
        conn = resolve(name)
        if conn.key in seen:
            continue
        seen.add(conn.key)

        if conn.kind == "venn":
            has_cred = bool(env.get(VENN_KEY_VAR) or os.environ.get(VENN_KEY_VAR))
            vc = (any(n in connected for n in conn.names())
                  if connected is not None else None)
        else:
            has_cred = bool(conn.credential_var and (
                env.get(conn.credential_var) or os.environ.get(conn.credential_var)))
            vc = None
        cards.append(card(conn, has_credential=has_cred, venn_connected=vc))
    return cards
