"""Registry of public **hosted MCP servers** — the Connect cascade's third rung.

When a service the team wants isn't native and isn't reachable through Venn,
setup checks this registry: many products now ship a *hosted* MCP server (a
URL you point a client at). A hit is wired straight into the team's
`agent.yaml` `mcp_servers:` block, so the agent connects to it at runtime —
no custom code, no authored guide. Only when a service is in none of these
(native / Venn / this registry) does it fall through to **custom** (capture an
API key + author a `tools/<svc>.md` guide), the "you'll need to build an MCP"
terminal state.

This module is **pure data + a lookup**: no imports from `services` (which
depends on *this*), no network. Each `MCPServerSpec` declares the hosted
endpoint and, where the server authenticates with a static API key, the env
var + header to send it in. Servers that use interactive OAuth carry no static
secret — the SDK authorizes them at first connect, so there's nothing for
setup to capture.

NOTE: hosted-MCP endpoints move; treat the URLs below as a seed to verify, not
gospel. Adding a server = adding an `MCPServerSpec` to `_SPECS`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MCPServerSpec:
    key: str                       # canonical id (matches a spec service name)
    name: str                      # display name
    url: str                       # the hosted MCP endpoint
    summary: str                   # what the team does with it
    transport: str = "http"        # "http" | "sse"
    scopes: tuple[str, ...] = ()   # human-readable granted capabilities
    docs_url: str = ""
    aliases: tuple[str, ...] = ()

    # Static API-key auth, when the server uses it. Empty `secret_var` means the
    # server authorizes via interactive OAuth at first connect — no secret here.
    secret_var: str = ""           # env var holding the key, e.g. STRIPE_API_KEY
    secret_label: str = ""
    secret_placeholder: str = ""
    secret_help: str = ""
    auth_header: str = "Authorization"   # header the key is sent in
    auth_value: str = "Bearer {ref}"     # {ref} → ${SECRET_VAR}
    setup_steps: tuple[str, ...] = ()    # human steps to get the key / authorize
    oauth_note: str = ""           # shown for OAuth servers (no static secret)

    def headers(self) -> dict:
        """The `headers:` block for `mcp_servers.<key>` — a single auth header
        referencing the key as `${VAR}` (interpolated from .env at config
        load), or empty for OAuth/public servers."""
        if not self.secret_var:
            return {}
        ref = "${" + self.secret_var + "}"
        return {self.auth_header: self.auth_value.format(ref=ref)}

    def server_config(self) -> dict:
        """The agent.yaml `mcp_servers.<key>` value: transport + url (+ headers
        when the server takes a static key)."""
        cfg: dict = {"type": self.transport, "url": self.url}
        h = self.headers()
        if h:
            cfg["headers"] = h
        return cfg


# The seed registry. Keys avoid anything Venn's curated buckets already cover
# (see venn.SERVICE_ALIASES) — those resolve to Venn first, by design.
_SPECS: list[MCPServerSpec] = [
    MCPServerSpec(
        key="stripe", name="Stripe",
        url="https://mcp.stripe.com",
        summary="Read and act on payments, customers, and invoices.",
        scopes=("read payments & customers", "create invoices/refunds"),
        docs_url="https://docs.stripe.com/mcp",
        secret_var="STRIPE_API_KEY",
        secret_label="Stripe API key",
        secret_placeholder="sk_live_… / rk_live_…",
        secret_help="A restricted key is safest — Stripe sends it as a Bearer "
                    "header to its hosted MCP.",
        setup_steps=(
            "In Stripe → Developers → API keys, create a (restricted) key.",
            "Paste it below — modastack wires Stripe's hosted MCP into the team "
            "and sends this key as its auth header.",
        ),
    ),
    MCPServerSpec(
        key="huggingface", name="Hugging Face",
        url="https://huggingface.co/mcp",
        summary="Search models/datasets and run inference.",
        scopes=("search models & datasets", "call inference endpoints"),
        docs_url="https://huggingface.co/settings/mcp",
        aliases=("hugging face", "hf"),
        secret_var="HF_TOKEN",
        secret_label="Hugging Face token",
        secret_placeholder="hf_…",
        secret_help="A read token is enough for most use; sent as a Bearer "
                    "header to the hosted MCP.",
        setup_steps=(
            "Create a token at huggingface.co/settings/tokens (read scope).",
            "Paste it below — modastack wires the Hugging Face hosted MCP in.",
        ),
    ),
    MCPServerSpec(
        key="sentry", name="Sentry",
        url="https://mcp.sentry.dev/mcp",
        summary="Triage issues, inspect events, and query errors.",
        scopes=("read issues & events", "query errors"),
        docs_url="https://docs.sentry.io/product/sentry-mcp/",
        oauth_note="Sentry's hosted MCP authorizes via OAuth at first connect — "
                   "no key to paste; you approve access when the team first runs.",
    ),
    MCPServerSpec(
        key="context7", name="Context7",
        url="https://mcp.context7.com/mcp",
        summary="Pull up-to-date library docs and code examples.",
        scopes=("fetch library docs", "fetch code examples"),
        docs_url="https://context7.com",
        oauth_note="Public hosted MCP — works without a key (rate-limited). "
                   "modastack wires it straight in.",
    ),
    MCPServerSpec(
        key="deepwiki", name="DeepWiki",
        url="https://mcp.deepwiki.com/mcp",
        summary="Ask questions about any public GitHub repo's docs.",
        scopes=("read repo documentation", "answer repo questions"),
        docs_url="https://deepwiki.com",
        oauth_note="Public hosted MCP — no key required. modastack wires it in.",
    ),
]

REGISTRY: dict[str, MCPServerSpec] = {s.key: s for s in _SPECS}

# Reverse index: every name/alias (lowercased) → canonical key. Aliases first,
# canonical keys second, so a real key always beats another server's alias.
_BY_NAME: dict[str, str] = {}
for _s in _SPECS:
    for _alias in _s.aliases:
        _BY_NAME.setdefault(_alias.lower(), _s.key)
for _s in _SPECS:
    _BY_NAME[_s.key.lower()] = _s.key


def lookup(name: str) -> MCPServerSpec | None:
    """Resolve a (possibly messy) service name to a hosted-MCP spec, or None."""
    key = _BY_NAME.get((name or "").strip().lower())
    return REGISTRY[key] if key else None
