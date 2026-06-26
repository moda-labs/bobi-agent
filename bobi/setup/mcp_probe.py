"""Live "does it actually connect?" test for a user-added MCP connection.

The setup chat (Bobi) is a no-tools design brain — it can't run the team's MCP
servers. So to let a user VERIFY a connection before moving on, we spawn the
server exactly as the team will at runtime and perform the MCP handshake:
`initialize` then `tools/list`. If tools come back, the connection is wired and
the server speaks MCP. Listing tools needs no credentials for well-behaved
servers (auth happens per tool call), so this checks the plumbing without side
effects — and it surfaces the tool names so the user sees what they'll get.

Read-only: we never call a tool (no writes, no data fetched). Returns
{"ok": True, "tools": [...], "count": N} or {"ok": False, "error": ..., ...}.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import anyio


# Only these ambient vars (and the connection's own declared ones) reach the
# child. We deliberately do NOT inherit all of os.environ — that would hand the
# spawned server every secret in the setup process (VENN_API_KEY, LINEAR_API_KEY,
# …). The child gets just enough to run (PATH/HOME, locale, proxies) plus the
# vars the connection itself declared.
_ENV_PASSTHROUGH = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM",
    "TMPDIR", "TMP", "TEMP", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy"))
_ENV_PASSTHROUGH_PREFIXES = ("XDG_", "LC_", "UV_")


def _resolved_env(entry: dict, project: Path) -> dict:
    """A MINIMAL child environment: a safe base (PATH/HOME/locale/proxies) plus
    only the connection's declared vars, read from .env (the saved secret) or the
    live environment. Other ambient secrets are intentionally withheld."""
    from bobi.setup.actions import read_env
    env = {k: v for k, v in os.environ.items()
           if k in _ENV_PASSTHROUGH or k.startswith(_ENV_PASSTHROUGH_PREFIXES)}
    saved = read_env(project)
    for var in entry.get("env_vars") or []:
        v = saved.get(var) or os.environ.get(var)
        if v:
            env[var] = v
    return env


def _tail(f, limit: int = 1500) -> str:
    """The tail of a captured stderr file — the server's own error output is
    usually the most useful part of a failure."""
    try:
        f.flush()
        f.seek(0)
        text = f.read()
    except Exception:
        return ""
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


# A real connection check calls one tool — but only a SAFE, read-only one with
# no required arguments, so we exercise credentials + network without writing
# anything or guessing arguments. The classifier is DEFAULT-DENY: a tool is only
# safe if a read verb leads its name AND no mutation word appears anywhere.
_READ_VERBS = frozenset((
    "get", "list", "read", "search", "fetch", "show", "describe", "find",
    "view", "query", "whoami", "ping", "health", "status", "count", "lookup",
    "preview", "summarize", "stat"))
# Any of these anywhere in the name disqualifies a tool — broad on purpose, since
# wrongly running a mutation is far worse than skipping a safe-but-unlisted tool.
_WRITE_HINTS = frozenset((
    "post", "create", "update", "delete", "send", "write", "publish", "set",
    "add", "remove", "cancel", "reset", "archive", "edit", "upsert", "put",
    "patch", "move", "rename", "clear", "drop", "approve", "merge", "close",
    "start", "stop", "run", "purge", "wipe", "truncate", "revoke", "destroy",
    "disable", "deactivate", "enable", "expire", "flush", "evict", "kill",
    "terminate", "deregister", "register", "uninstall", "install", "grant",
    "deny", "trigger", "invoke", "execute", "exec", "mutate", "ban", "block",
    "subscribe", "unsubscribe", "react", "vote", "like", "follow", "comment",
    "reply", "share", "import", "sync", "apply", "schedule", "restore",
    "rollback", "promote", "deploy", "release"))
# Tools most likely to work with no args and to actually hit the upstream API
# (so a green result means "credentials + network OK"), best first.
_PREFERRED = ("whoami", "me", "self", "ping", "health", "status", "feed",
              "notes", "subscriber", "stats", "list", "recent", "activity",
              "profile", "account", "user")


def _name_tokens(name: str) -> list[str]:
    """Lower-cased word tokens of a tool name, splitting snake_case, kebab-case,
    dotted namespaces AND camelCase — so `deleteAll`, `github.purge_repo`, and
    `list-and-wipe` all surface their verbs."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return [t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t]


def _is_read_only(name: str) -> bool:
    toks = _name_tokens(name)
    if not toks:
        return False
    # Any mutation word anywhere → not safe (e.g. `list_and_purge`, `get_or_delete`).
    if any(t in _WRITE_HINTS for t in toks):
        return False
    # Require a read verb to LEAD the name (allowing one server-namespace token,
    # e.g. `substack_get_notes_feed`), so a mutating tool that merely contains a
    # read word later (`purge_then_get`) is never treated as safe.
    return any(t in _READ_VERBS for t in toks[:2])


def _pick_safe_tool(tools):
    """A no-required-args, read-only tool to exercise the connection — or None
    if there isn't an obviously safe one (then we skip the live call)."""
    cands = [t for t in tools
             if not ((t.inputSchema or {}).get("required"))
             and _is_read_only(t.name)]
    if not cands:
        return None
    for pref in _PREFERRED:
        for t in cands:
            if pref in t.name.lower():
                return t
    return cands[0]


def _tool_error_text(out) -> str:
    try:
        parts = [getattr(c, "text", "") for c in (out.content or [])]
        text = " ".join(p for p in parts if p).strip()
        return text[:400] or "the tool returned an error"
    except Exception:  # noqa: BLE001
        return "the tool returned an error"


def _result_text(out) -> str:
    """A short snippet of a successful tool result, to show in chat."""
    try:
        parts = [getattr(c, "text", "") for c in (out.content or [])]
        text = " ".join(p for p in parts if p).strip()
        text = " ".join(text.split())   # collapse whitespace
        return text[:300]
    except Exception:  # noqa: BLE001
        return ""


async def _handshake(read, write, call_name) -> dict:
    """initialize + list tools, and — when `call_name` is given — call that one
    tool with no arguments to verify the connection end-to-end. Returns the tool
    list plus a `suggested` safe tool to propose, and (after a call) `live_ok` /
    `output` / `live_error`."""
    from mcp import ClientSession
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        suggested = _pick_safe_tool(tools)
        res = {"ok": True, "tools": [t.name for t in tools], "count": len(tools),
               "suggested": suggested.name if suggested else None,
               "called": None, "live_ok": None, "live_error": None,
               "output": None}
        if not call_name:
            return res
        res["called"] = call_name
        try:
            out = await session.call_tool(call_name, {})
            if getattr(out, "isError", False):
                res["live_ok"], res["live_error"] = False, _tool_error_text(out)
            else:
                res["live_ok"], res["output"] = True, _result_text(out)
        except Exception as e:  # noqa: BLE001
            res["live_ok"], res["live_error"] = False, str(e) or type(e).__name__
        return res


async def _probe_stdio(entry: dict, project: Path, timeout: float,
                       call_name) -> dict:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = (entry.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "this connection has no command to run."}
    # Build the child env off the event loop (it reads .env from disk).
    env = await anyio.to_thread.run_sync(_resolved_env, entry, project)
    params = StdioServerParameters(
        command=command,
        args=[str(a) for a in entry.get("args") or []],
        env=env)
    errlog = tempfile.TemporaryFile(mode="w+")
    try:
        with anyio.fail_after(timeout):
            async with stdio_client(params, errlog=errlog) as (read, write):
                return await _handshake(read, write, call_name)
    except TimeoutError:
        return {"ok": False,
                "error": f"timed out after {int(timeout)}s — a first run "
                         "resolves dependencies, so try once more.",
                "stderr": _tail(errlog)}
    except Exception as e:  # noqa: BLE001 — surface any launch/handshake failure
        return {"ok": False, "error": str(e) or type(e).__name__,
                "stderr": _tail(errlog)}
    finally:
        errlog.close()


async def _probe_http(entry: dict, project: Path, timeout: float,
                      call_name) -> dict:
    from mcp.client.streamable_http import streamablehttp_client
    from bobi.setup.actions import read_env
    url = (entry.get("url") or "").strip()
    headers: dict = {}
    if entry.get("auth") == "api_key" and entry.get("secret_var"):
        v = read_env(project).get(entry["secret_var"]) or \
            os.environ.get(entry["secret_var"])
        if v:
            headers["Authorization"] = f"Bearer {v}"
    try:
        with anyio.fail_after(timeout):
            async with streamablehttp_client(url, headers=headers) as streams:
                return await _handshake(streams[0], streams[1], call_name)
    except TimeoutError:
        return {"ok": False, "error": f"timed out after {int(timeout)}s."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e) or type(e).__name__}


# Unambiguous "test this connection" phrasings — these alone trigger a test.
_TEST_PHRASES = (
    "are we connected", "is it connected", "test the connection",
    "test connection", "test the mcp", "test my", "does it work",
    "is it working", "is the connection working", "verify the connection",
    "check the connection", "pull a note", "pull in a note", "can you reach",
    "are we hooked up", "is it hooked up", "make sure it works",
)
# Weaker signals — only count when paired with a connection name or "connection".
_TEST_VERBS = ("test", "verify", "check", "connected", "reachable", "working")


def match_connection_test(text: str, mcp_servers: dict) -> dict:
    """Detect a 'test my connection' intent in a chat message and resolve which
    connection it means. Returns {"intent": False} for ordinary design chat, or
    {"intent": True, ...} with one of: "key" (the connection to test), "none"
    (intent but nothing configured), or "ambiguous"+"candidates" (several, none
    named). Conservative — it should not hijack normal conversation."""
    t = (text or "").lower()
    servers = {k: v for k, v in (mcp_servers or {}).items()
               if isinstance(v, dict)}

    named = None
    for key, cfg in servers.items():
        label = (cfg.get("label") or key) or ""
        cands = {key.lower(), label.lower(),
                 label.lower().replace("-mcp", "").replace("_mcp", "").strip()}
        if any(len(c) >= 3 and c in t for c in cands):
            named = key
            break

    explicit = any(p in t for p in _TEST_PHRASES)
    verb = any(w in t for w in _TEST_VERBS)
    # Trigger on an explicit phrase, or a named connection paired with a verb.
    if not (explicit or (named and verb)):
        return {"intent": False}

    if named:
        return {"intent": True, "key": named}
    keys = list(servers)
    if not keys:
        return {"intent": True, "key": None, "none": True}
    if len(keys) == 1:
        return {"intent": True, "key": keys[0]}
    return {"intent": True, "key": None, "ambiguous": True,
            "candidates": [servers[k].get("label") or k for k in keys]}


_AFFIRM = frozenset((
    "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "go", "do", "run",
    "confirm", "y", "proceed", "please"))
_DECLINE = frozenset((
    "no", "nope", "cancel", "stop", "don't", "dont", "nevermind", "skip"))


def match_test_confirmation(text: str, pending: dict) -> dict:
    """Interpret the user's reply to a tool-call proposal. Returns
    {"action": "run", "tool": <name>} to run (the named tool, or the proposed
    one), {"action": "cancel"}, or {"action": "none"} when the reply isn't about
    the pending test (the caller then drops it and handles the message normally).

    Matching is deliberately narrow: an explicit tool name anywhere, OR the reply
    LEADS with an affirm/decline word. We never match bare words mid-sentence
    ("is the output ok?" must NOT run the tool), because the pending tool call
    has real effects on the user's live account."""
    t = (text or "").lower().strip()
    tools = pending.get("tools") or []
    first = re.split(r"[^a-z']+", t, maxsplit=1)[0] if t else ""
    # Decline FIRST, so "don't call substack_post_note" cancels — it must never
    # match the tool name and run a write.
    if first in _DECLINE:
        return {"action": "cancel"}
    # The user naming a specific tool → run it ONLY if it's read-only; a named
    # write tool is refused, never executed as a "test".
    for name in tools:
        if name.lower() in t:
            if _is_read_only(name):
                return {"action": "run", "tool": name}
            return {"action": "refuse_write", "tool": name}
    if first in _AFFIRM:
        return {"action": "run", "tool": pending.get("proposed")}
    return {"action": "none"}


def _scrub_result(result: dict, entry: dict, project: Path) -> dict:
    """Strip secrets from any human-facing text the probe surfaces (the server
    runs under the child's real credentials, and a misbehaving server can echo a
    cookie/token in its output or stderr). Replaces the connection's own secret
    values, then runs the shape-based redactor."""
    from bobi.setup.actions import read_env, redact_secrets
    saved = read_env(project)
    values = []
    for var in (entry.get("env_vars") or []) + ([entry["secret_var"]]
                                                if entry.get("secret_var") else []):
        v = saved.get(var) or os.environ.get(var)
        if v and len(v) >= 8:
            values.append(v)

    def scrub(text):
        if not text:
            return text
        for v in values:
            text = text.replace(v, "‹redacted›")
        return redact_secrets(text)[0]

    for k in ("output", "live_error", "error", "stderr"):
        if result.get(k):
            result[k] = scrub(result[k])
    return result


async def probe(entry: dict, project: Path, *, call_name: str | None = None,
                timeout: float = 60.0) -> dict:
    """Launch the connection and run the MCP handshake. With `call_name`, also
    invoke that one tool (no args) to verify the connection end-to-end.
    Dispatches on transport (stdio command vs remote URL)."""
    if entry.get("type") == "stdio" or entry.get("command"):
        result = await _probe_stdio(entry, project, timeout, call_name)
    elif entry.get("url"):
        result = await _probe_http(entry, project, timeout, call_name)
    else:
        return {"ok": False,
                "error": "connection has neither a command nor a URL to test."}
    return _scrub_result(result, entry, project)
