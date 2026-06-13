"""Subprocess wrapper around the external `venn` CLI for monitor discovery.

Discovery runs the same binary the generated `command:` monitor lines
will use at runtime, so a command that works here works verbatim in the
installed pack. The API key is injected into the subprocess environment
— it never appears in arguments or output, so it never reaches the
session transcript.

Discovery is read-only: `--confirm` (venn's write gate) is refused.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 8_000
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
