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

This module owns the WHOLE test-a-connection exchange, not just the probe:
the intent/confirmation matchers and the two dialogue turns that pair with
them (`propose_test`, `resolve_pending`). The turns used to be closures inside
the `/api/message` route, which split one conversation across two modules and
made it reachable only through a TestClient.
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
    only the connection's declared vars, resolved the way the running agent
    resolves them (`actions.env_value`). Other ambient secrets are
    intentionally withheld."""
    from bobi.setup.actions import env_value
    env = {k: v for k, v in os.environ.items()
           if k in _ENV_PASSTHROUGH or k.startswith(_ENV_PASSTHROUGH_PREFIXES)}
    for var in entry.get("env_vars") or []:
        v = env_value(project, var)
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


def _input_schema(tool) -> dict | None:
    """The tool's input schema across the mcp 2.0 field rename
    (``inputSchema`` → ``input_schema``; camelCase survives only as the wire
    alias). ``None`` means neither spelling exists — the caller must treat the
    schema as unknown, never as "no required arguments".

    camelCase is checked FIRST: on 1.x it is the declared field (always
    present, so the snake spelling is never consulted), while 2.0 never
    exposes it as an attribute. Snake-first would let a server-supplied extra
    wire key shadow the real field — 1.x models are ``extra="allow"``."""
    for attr in ("inputSchema", "input_schema"):
        if hasattr(tool, attr):
            return getattr(tool, attr) or {}
    return None


def _call_errored(out) -> bool:
    """Whether a tool call reported an error, across the mcp 2.0 field rename
    (``isError`` → ``is_error``). Fails loud when neither spelling exists —
    a silent default here reported an ERRORED call as live under mcp 2.0.
    camelCase first, for the same shadowing reason as ``_input_schema``."""
    for attr in ("isError", "is_error"):
        if hasattr(out, attr):
            return bool(getattr(out, attr))
    raise AttributeError("tool result has neither is_error nor isError")


def _pick_safe_tool(tools):
    """A no-required-args, read-only tool to exercise the connection — or None
    if there isn't an obviously safe one (then we skip the live call)."""
    cands = []
    for t in tools:
        schema = _input_schema(t)
        if schema is not None and not schema.get("required") \
                and _is_read_only(t.name):
            cands.append(t)
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
            if _call_errored(out):
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
    from bobi.mcp_handshake import open_streamable_http
    from bobi.setup.actions import env_value
    url = (entry.get("url") or "").strip()
    headers: dict = {}
    if entry.get("auth") == "api_key" and entry.get("secret_var"):
        v = env_value(project, entry["secret_var"])
        if v:
            headers["Authorization"] = f"Bearer {v}"
    try:
        with anyio.fail_after(timeout):
            async with open_streamable_http(url, headers=headers) as streams:
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
    values, then runs the shape-based redactor.

    Both candidate values are scrubbed — the exported one AND the one in
    `run/.env` — never just whichever `env_value` resolves to. A redactor that
    picked by precedence would blank the losing copy and echo the winning one
    verbatim the moment the two disagreed, which is precisely the case a
    redactor exists for."""
    from bobi.setup.actions import read_env, redact_secrets
    saved = read_env(project)
    values = []
    for var in (entry.get("env_vars") or []) + ([entry["secret_var"]]
                                                if entry.get("secret_var") else []):
        for v in (saved.get(var), os.environ.get(var)):
            if v and len(v) >= 8 and v not in values:
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


# --- the test-a-connection dialogue --------------------------------------
#
# Two turns, paired with the matchers above: `match_connection_test` routes a
# user message here, `propose_test` lists the server's tools and proposes a
# read-only one, `match_test_confirmation` reads the answer, and
# `resolve_pending` runs it. Both are plain async generators yielding text
# chunks — the route wraps them in SSE, so the whole conversation is testable
# without a TestClient.


def _record(state, project: Path, user_text: str, reply: str) -> None:
    state.messages.append({"role": "user", "content": user_text})
    state.messages.append({"role": "assistant", "content": reply})
    state.save(project)


def _probe_identity(entry: dict) -> dict:
    """The part of an MCP entry a connection test is actually about.

    Everything except the recorded outcome: if any of it changed while a probe
    was in flight, the result describes a different connection than the one the
    user now has.
    """
    return {k: v for k, v in (entry or {}).items() if k != "last_test"}


async def propose_test(state, project: Path, user_text: str, hit: dict):
    """First turn: launch the server, list its tools, and PROPOSE a safe
    read-only tool to call — the user confirms before anything runs."""
    if hit.get("none"):
        reply = ("There are no MCP connections set up yet to test. Add "
                 "one with “add a connection,” then ask me to test it.")
        yield reply
        _record(state, project, user_text, reply)
        return
    if hit.get("ambiguous"):
        reply = ("Which connection should I test? You have: "
                 + ", ".join(hit.get("candidates") or []) + ".")
        yield reply
        _record(state, project, user_text, reply)
        return
    key = hit["key"]
    entry = (state.spec.mcp_servers or {}).get(key) or {}
    label = entry.get("label") or key
    yield (f"Starting {label} and listing its tools (first run can take "
           "a moment)…\n\n")
    result = await probe(entry, project)   # list only, no call
    if not result.get("ok"):
        reply = f"✗ Couldn’t start {label}: {result.get('error')}"
        if result.get("stderr"):
            reply += f"\n\nServer output:\n{result['stderr'][:600]}"
        state.pending_test = {}
        yield reply
        _record(state, project, user_text, reply)
        return
    tools = result.get("tools") or []
    proposed = result.get("suggested")
    state.pending_test = {"key": key, "proposed": proposed, "tools": tools}
    state.save(project)
    shown = ", ".join(tools[:10]) + (" …" if len(tools) > 10 else "")
    if proposed:
        reply = (f"{label} is up — {len(tools)} tools available.\n\n"
                 f"To verify the connection end-to-end I'll call "
                 f"{proposed} (read-only, no arguments). Reply “yes” "
                 f"to run it, name another tool, or say no.\n\n"
                 f"Tools: {shown}")
    else:
        reply = (f"{label} is up — {len(tools)} tools available, but I "
                 f"couldn’t spot a clearly safe read-only one to call. "
                 f"Name a tool to try (no arguments will be sent): {shown}")
    yield reply
    _record(state, project, user_text, reply)


async def resolve_pending(state, project: Path, user_text: str, decision: dict):
    """Second turn: the user confirmed (or named a tool / declined).
    Run the chosen tool and report — this is the real connection test."""
    pending = state.pending_test or {}
    if decision["action"] == "cancel":
        state.pending_test = {}
        reply = "Okay — skipped the test. Ask again whenever you’re ready."
        yield reply
        _record(state, project, user_text, reply)
        return
    if decision["action"] == "refuse_write":
        # User named a tool that looks like it writes/changes data — never
        # run it as a connection test. Keep the proposal open.
        reply = (f"{decision.get('tool')} looks like it writes or changes "
                 f"data, so I won’t call it as a test. Pick a read-only "
                 f"tool, or reply “yes” to run the proposed one.")
        yield reply
        _record(state, project, user_text, reply)
        return
    tool = decision.get("tool")
    if not tool:
        reply = ("Name a tool to call (no arguments will be sent): "
                 + ", ".join(pending.get("tools") or []))
        yield reply
        _record(state, project, user_text, reply)
        return
    key = pending.get("key")
    entry = (state.spec.mcp_servers or {}).get(key)
    state.pending_test = {}
    # The connection may have been edited or removed between the proposal
    # and now — don't test a stale/empty key.
    if not isinstance(entry, dict) or not entry:
        reply = ("That connection isn’t there anymore — it may have been "
                 "removed or changed. Ask me to test it again.")
        yield reply
        _record(state, project, user_text, reply)
        return
    label = entry.get("label") or key
    yield f"Calling {tool} on {label}…\n\n"
    tested = _probe_identity(entry)
    result = await probe(entry, project, call_name=tool)
    # Re-read the entry AFTER the await. The probe can take up to 60s
    # (the first run resolves deps), and a user watching a slow test is
    # very likely editing the very connection being tested — fixing the
    # command that is failing. Writing the pre-probe snapshot back
    # reverted that correction silently, and re-added an entry deleted
    # mid-test. Worse than losing the edit: a result for the OLD command
    # would mark the NEW one connected, so a config that was never
    # tested renders green.
    current = (state.spec.mcp_servers or {}).get(key)
    if (not isinstance(current, dict) or not current
            or _probe_identity(current) != tested):
        reply = ("That connection changed while I was testing it, so "
                 "the result doesn’t apply to what you have now. Ask "
                 "me to test it again.")
        yield reply
        _record(state, project, user_text, reply)
        return
    # Persist ONLY coarse status — never raw error/stderr text, which can
    # carry secrets and is served to the browser via /api/state.
    current["last_test"] = {"ok": bool(result.get("ok")),
                            "live_ok": result.get("live_ok"),
                            "called": tool}
    state.spec.mcp_servers[key] = current
    if not result.get("ok"):
        reply = f"✗ Couldn’t start {label}: {result.get('error')}"
        if result.get("stderr"):
            reply += f"\n\nServer output:\n{result['stderr'][:600]}"
    elif result.get("live_ok"):
        out = (result.get("output") or "").strip()
        snippet = f"\n\nResponse: {out}" if out else ""
        reply = (f"✓ Called {tool} on {label} — it worked. The "
                 f"connection is live.{snippet}")
    else:
        reply = (f"⚠ {label} starts, but calling {tool} failed: "
                 f"{result.get('live_error')}\n\nThat usually means "
                 f"credentials aren’t set or aren’t valid yet — add them "
                 f"with “edit” on the connection, then re-test.")
    yield reply
    _record(state, project, user_text, reply)
