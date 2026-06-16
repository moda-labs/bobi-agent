"""Connector catalog for the Connect stage.

Connect is the deliberate exception to the magic: credential/access grants
must be *visible and approved*, so each implied service becomes a card that
states plainly what the team will be able to reach (granted scopes), how to
authorize it, and whether it's currently connected.

The catalog is **modular**: a connector is data, and adding one is adding a
`Connector` to the catalog — the Connect screen renders it generically. Each
connector offers one or more **auth methods**; a method bundles human setup
**steps** (e.g. "create a Slack app"), the **secrets** it captures (written to
`.env`, never sent to the model), and an optional **action** (Venn verify, an
external install link).

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
class Secret:
    """One credential to capture. Written to `.env` as `var`; never logged,
    never sent to the LLM."""
    var: str                       # env var name, e.g. SLACK_BOT_TOKEN
    label: str                     # human label, e.g. "Bot token"
    placeholder: str = ""          # input hint, e.g. "xoxb-…"
    help: str = ""                 # one-line note under the field
    optional: bool = False         # not required for the method to be satisfied


@dataclass(frozen=True)
class AuthMethod:
    """One way to connect a service: setup steps + the secrets it captures."""
    key: str                       # "token" | "app" | "venn" | …
    label: str                     # short label for the method picker
    summary: str = ""              # one line about this method
    steps: tuple[str, ...] = ()    # ordered, human setup instructions
    secrets: tuple[Secret, ...] = ()
    docs_url: str = ""             # "open the guide" link
    action: str = ""               # "venn" → verify via gateway; "" → plain capture


@dataclass(frozen=True)
class Connector:
    key: str                       # canonical id (matches a spec service name)
    name: str                      # display name
    kind: str                      # "native" | "venn"
    summary: str                   # what the team does with it
    scopes: tuple = ()             # human-readable granted capabilities
    methods: tuple = ()            # AuthMethod options (first is the default)
    aliases: tuple = ()            # alternate names that resolve here

    def names(self) -> set:
        return {self.key, *self.aliases}

    @property
    def credential_var(self) -> str:
        """The primary env var this connector captures (first required secret
        of the first method). Kept for manifest authoring, which references
        `${VAR}` for native services."""
        for m in self.methods:
            for s in m.secrets:
                if not s.optional:
                    return s.var
        for m in self.methods:
            for s in m.secrets:
                return s.var
        return ""


# --- shared method builders ----------------------------------------------

def _venn_method(name: str) -> AuthMethod:
    """The single 'connect via Venn' method shared by every venn connector —
    one API key unlocks them all; the underlying service is authorized in
    Venn's UI."""
    return AuthMethod(
        key="venn", label="Connect via Venn", action="venn",
        summary="Reached through the Venn gateway — one key for every service.",
        steps=(
            "Sign in at app.venn.ai and create an API key (Settings → API).",
            f"In Venn, connect your {name} account (one-click OAuth).",
            "Paste your Venn key below — it unlocks every Venn service at once.",
        ),
        secrets=(Secret(VENN_KEY_VAR, "Venn API key", "venn_…",
                        "One key for all Venn services."),),
        docs_url="https://app.venn.ai",
    )


# Native connectors — the framework has an ingestion adapter for each.
_NATIVE = [
    Connector(
        key="github", name="GitHub", kind="native",
        summary="Read and act on issues, pull requests, and reviews.",
        scopes=("read/write issues & PRs", "post comments", "receive webhooks"),
        methods=(
            AuthMethod(
                key="token", label="Access token",
                summary="Quickest — works locally right away.",
                steps=(
                    "Open github.com/settings/tokens → Generate new token.",
                    "Grant repo access: Contents, Issues, Pull requests "
                    "(read & write).",
                    "Copy the token and paste it below.",
                ),
                secrets=(Secret("GITHUB_TOKEN", "Personal access token",
                                "ghp_… / github_pat_…",
                                "Or skip — the team falls back to the gh CLI "
                                "already on this machine."),),
                docs_url="https://github.com/settings/tokens",
            ),
            AuthMethod(
                key="app", label="Install the GitHub App",
                summary="Best for webhooks — needs the cloud event server.",
                steps=(
                    "Install the modastack GitHub App on the repos to watch.",
                    "Its events flow to your event server, signed "
                    "automatically — no token to rotate.",
                    "Local-only setups should use an access token instead.",
                ),
                docs_url="https://github.com/apps/modastack",
            ),
        ),
    ),
    Connector(
        key="slack", name="Slack", kind="native",
        summary="Send and receive messages; reply in threads.",
        scopes=("post messages", "read channels the bot is in",
                "receive events"),
        methods=(
            AuthMethod(
                key="token", label="Bot token",
                summary="Create a small Slack app and paste its bot token.",
                steps=(
                    "Go to api.slack.com/apps → Create New App → From scratch.",
                    "Under OAuth & Permissions, add bot scopes: chat:write, "
                    "channels:history, channels:read, app_mentions:read.",
                    "Install to your workspace, then copy the Bot User OAuth "
                    "Token (starts xoxb-).",
                    "Invite the bot to a channel: /invite @your-bot.",
                ),
                secrets=(
                    Secret("SLACK_BOT_TOKEN", "Bot token", "xoxb-…"),
                    Secret("SLACK_SIGNING_SECRET", "Signing secret", "",
                           "Optional — only to receive events via the event "
                           "server.", optional=True),
                ),
                docs_url="https://api.slack.com/apps",
            ),
        ),
        aliases=("slackbot",),
    ),
    Connector(
        key="linear", name="Linear", kind="native",
        summary="Read and update issues, projects, and cycles.",
        scopes=("read/write issues", "receive webhooks"),
        methods=(
            AuthMethod(
                key="token", label="API key",
                summary="A personal API key; webhooks are optional.",
                steps=(
                    "Open Linear → Settings → API → Personal API keys → "
                    "New key.",
                    "Copy the key and paste it below.",
                    "For webhooks: Settings → API → Webhooks → point one at "
                    "your event server (optional).",
                ),
                secrets=(Secret("LINEAR_API_KEY", "API key", "lin_api_…"),),
                docs_url="https://linear.app/settings/api",
            ),
        ),
    ),
]

# Venn-backed connectors — reached through the Venn gateway. Keys mirror the
# coarse service buckets in venn.SERVICE_ALIASES so a spec that says "email"
# or "crm" resolves cleanly; aliases pull in the concrete product names.
_VENN_SPECS = [
    ("email", "Email", "Read and send mail.",
     ("read messages", "send messages")),
    ("calendar", "Calendar", "Read and create events.",
     ("read events", "create events")),
    ("docs", "Docs", "Read and edit documents.",
     ("read docs", "edit docs")),
    ("sheets", "Sheets", "Read and write spreadsheets.",
     ("read sheets", "write sheets")),
    ("storage", "File storage", "Read and write files in cloud storage.",
     ("list files", "read/write files")),
    ("crm", "CRM", "Read and update CRM records.",
     ("read records", "update records")),
    ("tickets", "Issue tracker", "Read and update tickets.",
     ("read tickets", "update tickets")),
]
_VENN = [
    Connector(key=key, name=name, kind="venn", summary=summary, scopes=scopes,
              methods=(_venn_method(name),),
              aliases=tuple(SERVICE_ALIASES.get(key, ())))
    for key, name, summary, scopes in _VENN_SPECS
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


# The real catalog of services Venn can reach (lowercased server names),
# BEYOND the curated buckets above. Seeded from a live `list_servers` dump and
# refreshed live when a key is present (see `live_venn_catalog`). A requested
# service that is neither native, a curated bucket, nor in this set is treated
# as a **custom** service: bobbi captures an API key and authors a tools guide.
#
# NOTE: this is the static seed for design-time (pre-key) classification. Keep
# it in sync with Venn's connector list; `live_venn_catalog` unions the user's
# live account catalog on top when their VENN_API_KEY is available.
VENN_CATALOG: frozenset[str] = frozenset({
    name.lower()
    for names in SERVICE_ALIASES.values() for name in names
})


def _env_var_for(name: str) -> str:
    """A credential env-var name for a custom service, e.g. 'PostHog' →
    POSTHOG_API_KEY."""
    import re
    s = re.sub(r"[^A-Z0-9]+", "_", (name or "").strip().upper()).strip("_")
    return f"{s or 'SERVICE'}_API_KEY"


def _custom_connector(name: str) -> Connector:
    """A service Venn doesn't cover: reached via its own API. bobbi captures an
    API key and writes it a `tools/<service>.md` usage guide at Build."""
    clean = (name or "").strip()
    var = _env_var_for(clean)
    method = AuthMethod(
        key="token", label="API key",
        summary=f"{clean or 'This service'} isn't built-in or on Venn — paste an "
                "API key and bobbi writes it a usage guide.",
        steps=(
            f"Create an API key in {clean or 'the service'} (usually under "
            "Settings → API).",
            "Paste it below. bobbi writes a tools/<service>.md guide so the "
            "agent knows how to call its API.",
        ),
        secrets=(Secret(var, f"{clean or 'Service'} API key", "",
                        f"Stored in .env; the agent reads it as ${{{var}}}."),),
    )
    return Connector(
        key=clean.lower(), name=clean or "service", kind="custom",
        summary="A custom service — reached via its own API, with a usage "
                "guide bobbi writes.",
        scopes=("as its API allows",),
        methods=(method,),
    )


def resolve(name: str, venn_catalog: frozenset | set | None = None) -> Connector:
    """Resolve a (possibly messy) service name to a Connector.

    Native and curated-bucket names resolve to their rich cards. Otherwise the
    name is checked against Venn's catalog (`venn_catalog`, defaulting to the
    static seed): a hit becomes a generic Venn connector; a miss becomes a
    **custom** connector (own API key + an authored tools guide).
    """
    key = _BY_NAME.get(name.strip().lower())
    if key:
        return CATALOG[key]
    clean = name.strip()
    catalog = venn_catalog if venn_catalog is not None else VENN_CATALOG
    if clean.lower() in catalog:
        return Connector(
            key=clean.lower(), name=clean or "service", kind="venn",
            summary="Reached through the Venn gateway.",
            scopes=("as granted in Venn",),
            methods=(_venn_method(clean or "this service"),),
        )
    return _custom_connector(clean)


# --- pure status logic ----------------------------------------------------

def _method_satisfied(method: AuthMethod, present: set, *,
                      venn_connected: bool | None) -> bool | None:
    """Is this method's auth complete? None = can't tell yet (venn unchecked).

    A method with no secrets (e.g. installing the GitHub App) can't be verified
    locally, so it's never auto-satisfied — the user picks a method that we can
    confirm (a captured token, or a live Venn connection)."""
    if method.action == "venn":
        return venn_connected   # True / False / None
    required = [s for s in method.secrets if not s.optional]
    if not required:
        return False            # external / unverifiable from here
    return all(s.var in present for s in required)


def _method_card(method: AuthMethod, present: set, *,
                 venn_connected: bool | None) -> dict:
    sat = _method_satisfied(method, present, venn_connected=venn_connected)
    return {
        "key": method.key,
        "label": method.label,
        "summary": method.summary,
        "steps": list(method.steps),
        "docs_url": method.docs_url,
        "action": method.action,
        "satisfied": sat,
        "secrets": [{
            "var": s.var, "label": s.label, "placeholder": s.placeholder,
            "help": s.help, "optional": s.optional,
            "present": s.var in present,
        } for s in method.secrets],
    }


def card(connector: Connector, *, present: set | None = None,
         venn_connected: bool | None = None) -> dict:
    """A serializable Connect card: the connector, its methods, and an overall
    status. `present` is the set of env var names already set; `venn_connected`
    is whether this service is live in the user's Venn account (None = not
    checked).

    Overall status:
      - connected — any method is satisfied
      - unknown   — nothing satisfied but a venn check is pending
      - missing   — otherwise (needs the user to act)
    """
    present = present or set()
    methods = [_method_card(m, present, venn_connected=venn_connected)
               for m in connector.methods]
    sats = [m["satisfied"] for m in methods]
    if any(s is True for s in sats):
        status = "connected"
    elif any(s is None for s in sats):
        status = "unknown"
    else:
        status = "missing"
    return {
        "key": connector.key,
        "name": connector.name,
        "kind": connector.kind,
        "summary": connector.summary,
        "scopes": list(connector.scopes),
        "methods": methods,
        "via": "Venn OAuth" if connector.kind == "venn" else "token",
        "status": status,
    }


def catalog_cards() -> list[dict]:
    """Every known connector as a status-less card — the 'add what was
    missed' picker for Connect."""
    return [card(c) for c in CATALOG.values()]


def _live_service_names(key: str) -> set[str]:
    """The real Venn catalog for this account. Prefers the canonical `venn`
    CLI (the same binary monitors run); falls back to the REST client when the
    CLI is absent or returns nothing (e.g. a TLS snag — the REST client pins
    certifi)."""
    from modastack.setup import venn_cli

    if venn_cli.venn_binary():
        names = venn_cli.list_service_names(key)
        if names:
            return names
    try:
        from modastack.venn import list_available_services
        return list_available_services(key)
    except Exception:
        return set()


def live_venn_catalog(project: Path) -> frozenset[str]:
    """The Venn catalog used for classifying services: the static seed, unioned
    with the user's live Venn account catalog when a key is present. Best-effort
    — falls back to the static seed if Venn can't be reached."""
    from modastack.setup.actions import venn_key

    key = venn_key(project)
    if not key:
        return VENN_CATALOG
    return VENN_CATALOG | frozenset(_live_service_names(key))


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


def _present_vars(connector: Connector, env: dict) -> set:
    """Which of this connector's secret vars are already set (env or process)."""
    present = set()
    for m in connector.methods:
        for s in m.secrets:
            if env.get(s.var) or os.environ.get(s.var):
                present.add(s.var)
    return present


def cards_for(service_names, project: Path,
              connected: set | None = None,
              catalog: frozenset | set | None = None) -> list[dict]:
    """Build Connect cards for the services the spec implies, deduped by
    connector. `connected` is a precomputed set of connected Venn server
    names (from `venn_connected_names`); None means "not checked". `catalog`
    is the Venn service catalog used to classify venn-vs-custom (the caller
    fetches it once via `live_venn_catalog`); None uses the static seed so
    pure/offline callers never hit the network.
    """
    from modastack.setup.actions import read_env

    env = read_env(project)
    cat = catalog if catalog is not None else VENN_CATALOG
    seen: set[str] = set()
    cards: list[dict] = []
    for raw in service_names:
        name = raw.get("name", "") if isinstance(raw, dict) else str(raw)
        if not name:
            continue
        conn = resolve(name, venn_catalog=cat)
        if conn.key in seen:
            continue
        seen.add(conn.key)

        if conn.kind == "venn":
            vc = (any(n in connected for n in conn.names())
                  if connected is not None else None)
        else:
            vc = None
        cards.append(card(conn, present=_present_vars(conn, env),
                          venn_connected=vc))
    return cards
