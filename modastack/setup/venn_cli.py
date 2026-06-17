"""Subprocess wrapper around the external `venn` CLI for monitor discovery.

Discovery runs the same binary the generated `command:` monitor lines
will use at runtime, so a command that works here works verbatim in the
installed pack. The API key is injected into the subprocess environment
— it never appears in arguments or output, so it never reaches the
session transcript.

Discovery is read-only: `--confirm` (venn's write gate) is refused.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 32_000
DEFAULT_TIMEOUT = 60


@dataclass
class VennResult:
    ok: bool
    output: str
    refused: str = ""    # non-empty when the call was blocked, with reason


def venn_binary() -> str | None:
    """Path to the venn CLI, or None when not installed."""
    return shutil.which("venn")


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… (truncated, {len(text)} chars total)"


def run_venn(args: str, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> VennResult:
    """Run `venn <args>` with the API key in the subprocess env.

    `args` is the argument string exactly as it would appear in a monitor
    `command:` line after the binary name (e.g.
    `tools execute -s work-gmail -t list_messages -a '{"maxResults": 5}'`).
    """
    binary = venn_binary()
    if not binary:
        return VennResult(
            ok=False, output="",
            refused=(
                "the `venn` CLI is not installed on this machine — fall back "
                "to description-only monitors and add a `requires:` entry for "
                "the venn CLI to agent.yaml"
            ),
        )

    try:
        argv = shlex.split(args)
    except ValueError as e:
        return VennResult(ok=False, output="", refused=f"unparseable arguments: {e}")
    if not argv:
        return VennResult(ok=False, output="", refused="no arguments given")
    if argv[0] == "venn":
        argv = argv[1:]

    if any(a == "--confirm" or a.startswith("--confirm=") for a in argv):
        return VennResult(
            ok=False, output="",
            refused=(
                "discovery is read-only: --confirm executes a write operation. "
                "Test write tools by describing their schema, not running them."
            ),
        )
    if any(a.startswith("--api-key") for a in argv):
        return VennResult(
            ok=False, output="",
            refused="never pass the API key as an argument — it is injected "
                    "into the environment automatically",
        )

    env = dict(os.environ)
    env["VENN_API_KEY"] = api_key
    try:
        proc = subprocess.run(
            [binary, *argv], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return VennResult(ok=False, output=f"timed out after {timeout}s")

    out = proc.stdout
    if proc.returncode != 0:
        out = (out + "\n" + proc.stderr).strip()
    return VennResult(ok=proc.returncode == 0, output=_truncate(out))


def list_servers(api_key: str, *, refresh: bool = False) -> list[dict]:
    """Venn's servers for this account, via the canonical `venn` CLI:
    `[{"name": str, "connected": bool}, ...]`. The full set — connected or not
    — is the catalog of services Venn can reach. This is the CLI port of the
    list-services capability; setup uses it as the source of the real Venn
    catalog (with the REST client in `modastack.venn` as a fallback).

    Read-only and defensive — returns [] on a missing binary, a refusal, a
    non-zero exit, or unparseable output.
    """
    args = "--json help list_servers" + (" --refresh" if refresh else "")
    res = run_venn(args, api_key)
    if not res.ok:
        return []
    try:
        data = json.loads(res.output)
    except (ValueError, TypeError):
        return []
    servers = ((data or {}).get("result") or {}).get("servers") or []
    out: list[dict] = []
    for s in servers:
        if not isinstance(s, dict):
            continue
        name = (s.get("server_name") or "").strip()
        if name:
            out.append({"name": name, "connected": bool(s.get("connected"))})
    return out


def list_service_names(api_key: str) -> set[str]:
    """Lowercased names of every service Venn supports for this account."""
    return {s["name"].lower() for s in list_servers(api_key)}
