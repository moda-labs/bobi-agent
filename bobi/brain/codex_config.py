"""Render team-brought MCP servers into Codex's ``~/.codex/config.toml`` (#428
Stage 4).

Claude reads its MCP servers from a per-session option (``subagent.py`` splats
``cfg.mcp_servers`` into the SDK), so the compose-time emission in
``tool_library`` is all Claude needs. Codex is different: ``codex exec`` reads
MCP servers from ``~/.codex/config.toml`` at process start, forwarding nothing
from the CLI invocation. So a codex-brained team's effective ``mcp_servers`` has
to be *rendered to disk* before any ``codex`` process runs.

The input is the SDK-native ``{name: spec}`` shape carried on the composed
agent.yaml (``type: stdio|http|sse``; stdio ``command``/``args``/``env``; http/
sse ``url``/``headers``). The output is the Codex config shape, authoritatively
matched against ``codex mcp add`` / ``codex mcp list --json`` on codex
``rust-v0.142``:

    [mcp_servers.<name>]
    command = "..."           # stdio
    args = ["..."]
    [mcp_servers.<name>.env]
    KEY = "VALUE"

    [mcp_servers.<name>]
    url = "https://..."       # http/sse -> streamable_http
    [mcp_servers.<name>.http_headers]
    Authorization = "Bearer ..."

The rendered tables live inside a **managed block** so any non-MCP settings a
user (or a later boot) put in ``config.toml`` survive verbatim — bobi owns only
the ``mcp_servers`` it renders, nothing else in the file.
"""

from __future__ import annotations

import os
from pathlib import Path

# Sentinels bracketing the bobi-owned region of config.toml. Everything outside
# them is foreign content preserved untouched; everything between them is
# regenerated on each render (idempotent).
MANAGED_BEGIN = "# >>> bobi-managed mcp_servers (do not edit) >>>"
MANAGED_END = "# <<< bobi-managed mcp_servers <<<"

# Codex reads its config from $CODEX_HOME (default ~/.codex). The entrypoint
# symlinks ~/.codex at the durable volume, so writing there persists.
_CONFIG_FILENAME = "config.toml"


def codex_home() -> Path:
    """The directory Codex loads ``config.toml`` from (``$CODEX_HOME`` or ~/.codex)."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def _bare_key(key: str) -> bool:
    """True if *key* is a TOML bare key (needs no quoting): ``A-Za-z0-9_-``."""
    return bool(key) and all(
        c.isascii() and (c.isalnum() or c in "_-") for c in key)


def _toml_key(key: str) -> str:
    """A single dotted-path segment, quoted only when not a bare key."""
    return key if _bare_key(key) else _toml_str(key)


def _toml_str(value: str) -> str:
    """A TOML basic string (double-quoted, minimal escaping)."""
    out = [
        '\\"' if c == '"'
        else "\\\\" if c == "\\"
        else "\\n" if c == "\n"
        else "\\r" if c == "\r"
        else "\\t" if c == "\t"
        else c
        for c in str(value)
    ]
    return '"' + "".join(out) + '"'


def _toml_value(value) -> str:
    """Render a scalar or list-of-scalars as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_str(value)


def _server_tables(name: str, spec: dict) -> list[str]:
    """The TOML tables for one MCP server, in the Codex config shape."""
    head = f"[mcp_servers.{_toml_key(name)}]"
    lines = [head]
    sub: list[str] = []  # sub-tables (env/http_headers) emitted after the head

    server_type = str(spec.get("type") or "stdio")
    if server_type in ("http", "sse"):
        url = spec.get("url")
        if url:
            lines.append(f"url = {_toml_str(url)}")
        headers = spec.get("headers")
        if isinstance(headers, dict) and headers:
            sub.append("")
            sub.append(f"[mcp_servers.{_toml_key(name)}.http_headers]")
            for hk, hv in headers.items():
                sub.append(f"{_toml_key(hk)} = {_toml_str(hv)}")
    else:  # stdio
        command = spec.get("command")
        if command:
            lines.append(f"command = {_toml_str(command)}")
        args = spec.get("args")
        if args:
            lines.append(f"args = {_toml_value(list(args))}")
        env = spec.get("env")
        if isinstance(env, dict) and env:
            sub.append("")
            sub.append(f"[mcp_servers.{_toml_key(name)}.env]")
            for ek, ev in env.items():
                sub.append(f"{_toml_key(ek)} = {_toml_str(ev)}")

    return lines + sub


def render_mcp_tables(mcp_servers: dict) -> str:
    """Render ``{name: spec}`` into Codex ``[mcp_servers.*]`` TOML tables.

    Deterministic (servers sorted by name) so the rendered config is stable
    across boots. Returns "" for an empty/absent mapping.
    """
    if not mcp_servers:
        return ""
    blocks: list[str] = []
    for name in sorted(mcp_servers):
        blocks.append("\n".join(_server_tables(name, mcp_servers[name] or {})))
    return "\n\n".join(blocks)


def _is_mcp_header(stripped: str) -> bool:
    """True if *stripped* is a ``[mcp_servers...]`` / ``[[mcp_servers...]]`` table
    header (the namespace bobi owns), but not a lookalike like ``[mcp_servers_x]``."""
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return False
    inner = stripped.strip("[]").strip()
    top = inner.split(".", 1)[0].strip().strip('"')
    return top == "mcp_servers"


def _strip_managed(text: str) -> str:
    """Remove bobi's markers and **every** ``[mcp_servers...]`` table, preserving
    all other content.

    bobi owns the whole ``mcp_servers`` namespace in config.toml, so any stray
    MCP table (a stale render or a manual ``codex mcp add``) is dropped — this is
    what keeps a re-render from producing a duplicate table that fails TOML
    parsing. Non-MCP settings (``model``, ``[history]``, …) survive verbatim.
    """
    out: list[str] = []
    in_mcp = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s in (MANAGED_BEGIN, MANAGED_END):
            continue
        if s.startswith("["):  # a table header re-decides the region
            in_mcp = _is_mcp_header(s)
            if in_mcp:
                continue
        if in_mcp:
            continue
        out.append(line)
    return "\n".join(out)


def config_has_managed_block(home: Path | None = None) -> bool:
    """True if ``<home>/config.toml`` carries a bobi-managed block — the signal
    that a stale MCP render needs cleaning even when the team now declares none."""
    home = home or codex_home()
    path = home / _CONFIG_FILENAME
    return path.is_file() and MANAGED_BEGIN in path.read_text()


def render_config(existing: str, mcp_servers: dict) -> str:
    """Render the ``mcp_servers`` block into *existing* config.toml text.

    Every existing ``[mcp_servers...]`` table (managed or stray) is removed and
    the current set re-rendered inside a managed block appended at the end
    (tables-after-tables is always valid TOML). Non-MCP content is preserved. An
    empty server set removes the block entirely.
    """
    foreign = _strip_managed(existing or "").rstrip("\n")
    tables = render_mcp_tables(mcp_servers)
    if not tables:
        return foreign + "\n" if foreign else ""
    block = f"{MANAGED_BEGIN}\n{tables}\n{MANAGED_END}"
    return (foreign + "\n\n" if foreign else "") + block + "\n"


def write_codex_config(mcp_servers: dict, home: Path | None = None) -> Path:
    """Render *mcp_servers* into ``<home>/config.toml``, preserving foreign keys.

    Idempotent: re-rendering the same set reproduces the same file. Returns the
    config path. ``home`` defaults to :func:`codex_home`.
    """
    home = home or codex_home()
    path = home / _CONFIG_FILENAME
    existing = path.read_text() if path.is_file() else ""
    rendered = render_config(existing, mcp_servers or {})
    # Only touch disk when the content actually changes (avoids churning the
    # durable volume + mtime on every session spawn).
    if rendered != existing:
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return path
