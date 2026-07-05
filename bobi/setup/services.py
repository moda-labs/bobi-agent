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

A service classifies into one of four kinds, in a deliberate cascade
(`resolve`): native → venn → mcp → custom.
  - **native** — the framework ships an ingestion adapter (github / slack /
    linear). Reached with a direct token in `.env`; events arrive by webhook.
  - **venn** — reached through the Venn gateway with the shared `VENN_API_KEY`
    ("one key, every service"); the user authorizes the underlying service via
    Venn's OAuth, and connection is verified against Venn's connected servers.
  - **mcp** — a service Venn doesn't cover but that ships a **hosted MCP
    server** (`bobi/setup/mcp_registry.py`). Wired straight into the team's
    `agent.yaml` `mcp_servers:` block; a static-key server captures one secret,
    an OAuth server authorizes at first connect.
  - **custom** — none of the above. bobi captures an `<SVC>_API_KEY` and
    authors a `tools/<svc>.md` usage guide — the "you'll need an MCP for this"
    terminal state.

This module is the *catalog + pure status logic*. Live Venn lookups and
`.env` reads are thin wrappers around it (the web server calls them once per
Connect render); the pure `card()` is what the tests pin down.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from bobi.venn import SERVICE_ALIASES

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
    kind: str                      # "native" | "venn" | "mcp" | "custom"
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
                    "Install the bobi GitHub App on the repos to watch.",
                    "Its events flow to your event server, signed "
                    "automatically — no token to rotate.",
                    "Local-only setups should use an access token instead.",
                ),
                docs_url="https://github.com/apps/bobi",
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
                summary="Create a Slack app from a manifest, paste its bot "
                        "token.",
                steps=(
                    "Run `bobi create-slack-bot` and open the printed "
                    "create link — scopes + events are prefilled.",
                    "Or create from the manifest: api.slack.com/apps → Create "
                    "New App → From a manifest.",
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
                    "your event server, then copy its signing secret below "
                    "(optional).",
                ),
                secrets=(
                    Secret("LINEAR_API_KEY", "API key", "lin_api_…"),
                    Secret("LINEAR_WEBHOOK_SECRET", "Webhook signing secret", "",
                           "Optional; only to verify Linear events via the "
                           "event server.", optional=True),
                ),
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
# as a **custom** service: bobi captures an API key and authors a tools guide.
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
    """A service Venn doesn't cover: reached via its own API. bobi captures an
    API key and writes it a `tools/<service>.md` usage guide at Build."""
    clean = (name or "").strip()
    var = _env_var_for(clean)
    method = AuthMethod(
        key="token", label="API key",
        summary=f"{clean or 'This service'} isn't built-in or on Venn — paste an "
                "API key and bobi writes it a usage guide.",
        steps=(
            f"Create an API key in {clean or 'the service'} (usually under "
            "Settings → API).",
            "Paste it below. bobi writes a tools/<service>.md guide so the "
            "agent knows how to call its API.",
        ),
        secrets=(Secret(var, f"{clean or 'Service'} API key", "",
                        f"Stored in .env; the agent reads it as ${{{var}}}."),),
    )
    return Connector(
        key=clean.lower(), name=clean or "service", kind="custom",
        summary="A custom service — reached via its own API, with a usage "
                "guide bobi writes.",
        scopes=("as its API allows",),
        methods=(method,),
    )


def _mcp_connector(spec) -> Connector:
    """A service Venn doesn't cover but that ships a hosted MCP server: wired
    into agent.yaml `mcp_servers:`. A static-key server captures one secret; an
    OAuth/public server captures nothing (authorized at first connect)."""
    secrets = ()
    if spec.secret_var:
        secrets = (Secret(
            spec.secret_var, spec.secret_label or f"{spec.name} API key",
            spec.secret_placeholder,
            spec.secret_help
            or f"Stored in .env; sent to {spec.name}'s hosted MCP as a header."),)
    steps = spec.setup_steps or (
        (spec.oauth_note,) if spec.oauth_note else ())
    method = AuthMethod(
        key="mcp", label="Connect the hosted MCP", action="mcp",
        summary=(spec.summary
                 or f"{spec.name} ships a hosted MCP — bobi wires it in."),
        steps=tuple(s for s in steps if s),
        secrets=secrets,
        docs_url=spec.docs_url,
    )
    return Connector(
        key=spec.key, name=spec.name, kind="mcp",
        summary=spec.summary or "Reached through its hosted MCP server.",
        scopes=tuple(spec.scopes), methods=(method,),
        aliases=tuple(spec.aliases),
    )


# The generic Venn bucket terms (the SERVICE_ALIASES keys). Said literally
# ("email", "crm"), these resolve to the broad bucket card; a *concrete* service
# name (gmail, salesforce, or a custom Venn connection name) keeps its own
# identity instead of collapsing into the bucket.
_BUCKET_KEYS: frozenset[str] = frozenset(spec[0] for spec in _VENN_SPECS)


def _display_name(name: str) -> str:
    """A presentable label for a concrete service. Keeps a name Venn already
    cased ("Work Gmail"); title-cases a bare slug ("gmail" → "Gmail",
    "google_calendar" → "Google Calendar")."""
    import re
    n = (name or "").strip()
    if not n:
        return "service"
    if any(ch.isupper() for ch in n):
        return n
    return re.sub(r"[_\-]+", " ", n).title()


def _venn_named(name: str) -> Connector:
    """A Venn connector that keeps its CONCRETE name — so each Venn connection
    (even two Gmails with different names) is its own row, shown as "Gmail",
    not the bucket label "Email"."""
    clean = name.strip()
    return Connector(
        key=clean.lower(), name=_display_name(clean), kind="venn",
        summary="Reached through the Venn gateway.",
        scopes=("as granted in Venn",),
        methods=(_venn_method(_display_name(clean)),),
    )


def resolve(name: str, venn_catalog: frozenset | set | None = None) -> Connector:
    """Resolve a (possibly messy) service name to a Connector, via the cascade
    native → venn → mcp → custom.

    Native names resolve to their rich cards. A generic bucket term ("email",
    "crm") resolves to the broad Venn bucket card. A concrete Venn name — a
    known alias (gmail, salesforce) or anything in Venn's catalog (`venn_catalog`,
    defaulting to the static seed) — resolves to a Venn connector that KEEPS its
    own name, so two Gmail connections are two distinct, correctly-labelled rows.
    A non-Venn miss is checked against the **hosted-MCP registry** (wired into
    `mcp_servers:`); a miss there becomes a **custom** connector.
    """
    from bobi.setup import mcp_registry

    clean = name.strip()
    low = clean.lower()
    key = _BY_NAME.get(low)
    if key:
        conn = CATALOG[key]
        if conn.kind == "native":
            return conn               # github / slack / linear win outright
        if low in _BUCKET_KEYS:
            return conn               # a literal bucket term → the bucket card
        return _venn_named(clean)     # a concrete venn alias → keep its name
    catalog = venn_catalog if venn_catalog is not None else VENN_CATALOG
    if low in catalog:
        return _venn_named(clean)
    spec = mcp_registry.lookup(clean)
    if spec:
        return _mcp_connector(spec)
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
    if method.action == "mcp":
        # A hosted MCP is wired deterministically into agent.yaml — there's
        # nothing the user must do here beyond an API key when the server takes
        # one. So it's satisfied once any required key is present; an OAuth/
        # public server (no required secret) is satisfied outright.
        return all(s.var in present for s in required)
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
        "via": {"venn": "Venn OAuth", "mcp": "hosted MCP"}.get(
            connector.kind, "token"),
        "status": status,
    }


def catalog_cards() -> list[dict]:
    """Every known connector as a status-less card — the 'add what was
    missed' picker for Connect."""
    return [card(c) for c in CATALOG.values()]


def canonical_service_key(key: str) -> str:
    """A loose identity for a connection so a service first guessed by bare name
    ('substack') and the MCP a user later adds for it ('substack-mcp' →
    'substack_mcp') collapse to ONE card. Strips non-alphanumerics and any
    leading/trailing 'mcp'/'server' qualifier. Falls back to the lower-cased key
    when stripping would leave nothing (e.g. a server literally named 'mcp')."""
    k = re.sub(r"[^a-z0-9]+", "", (key or "").lower())
    changed = True
    while changed:
        changed = False
        for q in ("mcp", "server"):
            if k.endswith(q) and len(k) > len(q):
                k = k[:-len(q)]
                changed = True
        if k.startswith("mcp") and len(k) > 3:
            k = k[3:]
            changed = True
    return k or (key or "").lower()


def user_mcp_card(key: str, cfg: dict, project: Path) -> dict:
    """A Connect card for a user-defined custom MCP connection — either a remote
    server (name + URL) or a local command-based one (name + command, stdio).
    We do NOT verify the connection here — the agent actually connects at
    `bobi agent <name> start`, where mcp_servers are probed. So this never claims
    "connected"; it reports what's been set and flags what's still needed."""
    from bobi.setup.actions import read_env
    auth = cfg.get("auth", "none")
    label = cfg.get("label") or _display_name(key)
    # Local command (stdio) server: summary is the command line; the "auth" it
    # needs is its declared env vars. Missing any → still incomplete.
    if cfg.get("type") == "stdio" or cfg.get("command"):
        env = read_env(project)
        env_vars = cfg.get("env_vars") or []
        missing = [v for v in env_vars
                   if not (env.get(v) or os.environ.get(v))]
        cmd = " ".join([cfg.get("command", ""),
                        *(str(a) for a in cfg.get("args") or [])]).strip()
        if missing:
            status, note = "needs_auth", "needs env: " + ", ".join(missing)
        else:
            status, note = "added", "local command · test it from chat"
        card = {
            "key": key.strip().lower(), "name": label, "kind": "mcp",
            "summary": cmd, "scopes": [], "methods": [],
            "via": "local command", "status": status, "user_mcp": True,
            "auth": "stdio", "url": "", "note": note,
        }
        return _overlay_last_test(card, cfg.get("last_test"))
    if auth == "api_key":
        var = cfg.get("secret_var", "")
        present = bool(read_env(project).get(var) or os.environ.get(var))
        # No key → genuinely incomplete; a key → set, but still unverified.
        status = "added" if present else "needs_auth"
        note = ("API key set · test it from chat" if present
                else "needs an API key")
    else:
        status, note = "added", "no auth (public server)"
    card = {
        "key": key.strip().lower(), "name": label, "kind": "mcp",
        "summary": cfg.get("url", ""), "scopes": [], "methods": [],
        "via": "hosted MCP", "status": status, "user_mcp": True,
        "auth": auth, "url": cfg.get("url", ""), "note": note,
    }
    return _overlay_last_test(card, cfg.get("last_test"))


def _overlay_last_test(card: dict, last_test) -> dict:
    """Reflect the most recent live tool-call test on the card. A successful call
    ('live_ok') marks it 'connected'; a failed call or a server that wouldn't
    start marks it 'error'. Without a test it stays 'added'/'needs_auth'
    (pending). A pass only upgrades the optimistic 'added' — a connection still
    'needs_auth' (missing config) keeps that, so a stale pass can't mask
    now-missing credentials."""
    if not isinstance(last_test, dict):
        return card
    server_ok = last_test.get("ok")
    live_ok = last_test.get("live_ok")
    if server_ok and live_ok:
        if card["status"] == "added":      # don't override a needs_auth card
            card["status"] = "connected"
            tool = last_test.get("called")
            card["note"] = f"verified · {tool}" if tool else "connected"
    elif server_ok is False or live_ok is False:
        card["status"] = "error"
        card["note"] = "test failed — re-test from chat"
    return card


def _live_service_names(key: str) -> set[str]:
    """The real Venn catalog for this account. Prefers the canonical `venn`
    CLI (the same binary monitors run); falls back to the REST client when the
    CLI is absent or returns nothing (e.g. a TLS snag — the REST client pins
    certifi)."""
    from bobi.setup import venn_cli

    if venn_cli.venn_binary():
        names = venn_cli.list_service_names(key)
        if names:
            return names
    try:
        from bobi.venn import list_available_services
        return list_available_services(key)
    except Exception:
        return set()


def live_venn_catalog(project: Path) -> frozenset[str]:
    """The Venn catalog used for classifying services: the static seed, unioned
    with the user's live Venn account catalog when a key is present. Best-effort
    — falls back to the static seed if Venn can't be reached."""
    from bobi.setup.actions import venn_key

    key = venn_key(project)
    if not key:
        return VENN_CATALOG
    return VENN_CATALOG | frozenset(_live_service_names(key))


def venn_connected_names(project: Path, key: str | None = None) -> set[str] | None:
    """Lowercased names of services currently connected in the user's Venn
    account, or None when no key is available / Venn can't be reached.

    Thin live wrapper — the pure status logic lives in `card()`.
    """
    from bobi.setup.actions import venn_key
    from bobi.venn import list_servers

    key = key or venn_key(project)
    if not key:
        return None
    try:
        servers = list_servers(key)
    except Exception:
        return None
    return {s.server_name.lower() for s in servers if s.connected}


def _with_declared_vars(conn: Connector,
                        declared: dict[str, str]) -> Connector:
    """A copy of `conn` whose secret var names are replaced by the ${VAR}
    names the team's own agent.yaml declares — the pack is authoritative
    for naming; the catalog vars are only authoring defaults for packs
    setup writes itself.

    Mapping rule per secret: a declared credential key matches a catalog
    secret whose var name contains it (bot_token → SLACK_BOT_TOKEN); if the
    pack declares exactly one var and the method has exactly one required
    secret, they pair directly. Unmatched secrets keep their catalog name.
    """
    from dataclasses import replace

    if not declared:
        return conn

    def _map_secret(secret: Secret, method: AuthMethod) -> Secret:
        for key, var in declared.items():
            if key.upper() in secret.var.upper():
                return replace(secret, var=var)
        required = [s for s in method.secrets if not s.optional]
        if len(declared) == 1 and len(required) == 1 and secret is required[0]:
            return replace(secret, var=next(iter(declared.values())))
        return secret

    methods = tuple(
        replace(m, secrets=tuple(_map_secret(s, m) for s in m.secrets))
        for m in conn.methods
    )
    return replace(conn, methods=methods)


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
    from bobi.setup.actions import read_env

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
        if isinstance(raw, dict) and raw.get("credential_vars"):
            # An opened/template pack declares its own ${VAR} names —
            # capture under those, not the catalog's authoring defaults.
            conn = _with_declared_vars(conn, raw["credential_vars"])

        if conn.kind == "venn":
            vc = (any(n in connected for n in conn.names())
                  if connected is not None else None)
        else:
            vc = None
        cards.append(card(conn, present=_present_vars(conn, env),
                          venn_connected=vc))
    return cards
