"""Tool-agnostic poll checks — $0 LLM native check runners.

General-purpose check runners that execute CLI commands to pull items,
normalize the result to {id, ...} conditions, and return them for the
scheduler's _reconcile path. Works with any CLI tool (Venn, custom
scripts, MCP servers via CLI wrappers, etc.).

Script caching: on first successful poll the resolved command is saved
as a shell script.  Subsequent runs try the cached script first.  If it
fails, the runner falls back to direct execution and regenerates the
cache — self-healing with zero agent involvement for mechanical repairs.

Check runners:
    tool_poll  — general-purpose: runs monitor.extra['command'], parses JSON
    venn_poll  — convenience: builds the venn CLI command from service/tool/query

Example monitor YAML (tool_poll — any command):
    - name: unread-emails
      check: tool_poll
      interval: 5m
      event: monitor/email.received
      command: 'venn tools execute -s work-gmail -t list_messages -a ''{"maxResults": 10}'''
      id_field: id

Example monitor YAML (venn_poll — Venn shorthand):
    - name: email-watch
      check: venn_poll
      interval: 5m
      event: monitor/email.received
      service: work-gmail
      tool: list_messages
      query: '{"maxResults": 10, "q": "is:unread"}'
      id_field: id
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

from modastack.monitors.schema import Condition

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Script cache
# ---------------------------------------------------------------------------

def _scripts_dir() -> Path:
    """Directory for cached monitor scripts."""
    from modastack import paths
    d = paths.state_dir() / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _script_path(monitor_name: str) -> Path:
    return _scripts_dir() / f"{monitor_name}.sh"


def _cache_script(monitor_name: str, cmd_parts: list[str]) -> None:
    """Save the resolved command as a shell script for future runs."""
    path = _script_path(monitor_name)
    # Quote each part for safe shell execution
    quoted = " ".join(shlex.quote(p) for p in cmd_parts)
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{quoted}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_cached_script(monitor_name: str, timeout: int, env: dict) -> subprocess.CompletedProcess | None:
    """Try running the cached script.  Returns None if no script exists."""
    path = _script_path(monitor_name)
    if not path.exists():
        return None
    try:
        return subprocess.run(
            [str(path)], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


# ---------------------------------------------------------------------------
# Shared execution + parsing
# ---------------------------------------------------------------------------

def _parse_items(stdout: str, monitor_name: str) -> list[dict] | None:
    """Parse JSON output into a list of item dicts, or None on failure."""
    stdout = stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning(f"tool_poll monitor {monitor_name}: non-JSON output")
        return None

    # Normalize: {"result": [...]}, plain list, or single object
    if isinstance(data, dict):
        items = data.get("result", [])
        if not isinstance(items, list):
            items = [items] if items else []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    return items


def _items_to_conditions(items: list[dict], id_field: str) -> list[Condition]:
    """Convert parsed item dicts to Condition objects."""
    conditions: list[Condition] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get(id_field)
        if raw_id is not None:
            key = str(raw_id)
        else:
            key = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()
            ).hexdigest()[:12]
        conditions.append(Condition(key=key, data=item))
    return conditions


def _run_command(cmd: list[str], env: dict, timeout: int,
                 monitor_name: str, id_field: str,
                 *, cache_scripts: bool = True) -> list[Condition] | None:
    """Run a command, parse JSON output to conditions.

    With cache_scripts=True (default), the resolved command is cached as a
    script on success and the cached script is tried first on the next run.
    """
    # Try cached script first
    if cache_scripts:
        cached = _run_cached_script(monitor_name, timeout, env)
        if cached is not None and cached.returncode == 0:
            items = _parse_items(cached.stdout, monitor_name)
            if items is not None:
                return _items_to_conditions(items, id_field)
            # Cached script returned garbage — fall through to direct execution
            log.info(f"tool_poll monitor {monitor_name}: cached script returned "
                     "unparseable output — falling back to direct execution")

    # Direct execution
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        log.error(f"tool_poll monitor {monitor_name}: timed out after {timeout}s")
        return None
    except OSError as e:
        log.error(f"tool_poll monitor {monitor_name}: failed to run: {e}")
        return None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        log.warning(f"tool_poll monitor {monitor_name}: exit {result.returncode}: {stderr}")
        # Invalidate cached script on failure
        if cache_scripts:
            sp = _script_path(monitor_name)
            if sp.exists():
                sp.unlink()
        return None

    items = _parse_items(result.stdout, monitor_name)
    if items is None:
        return None

    # Cache the command for future runs
    if cache_scripts:
        try:
            _cache_script(monitor_name, cmd)
        except OSError as e:
            log.debug(f"tool_poll monitor {monitor_name}: couldn't cache script: {e}")

    return _items_to_conditions(items, id_field)


# ---------------------------------------------------------------------------
# tool_poll — general-purpose runner
# ---------------------------------------------------------------------------

def tool_poll(monitor, projects: list[Path]) -> list[Condition] | None:
    """General-purpose poll: runs monitor.extra['command'] and parses output.

    Returns a list of Condition objects (possibly empty = all clear),
    or None when the poll failed (indeterminate).

    Required monitor.extra fields:
        command: Shell command string that outputs JSON
    Optional:
        id_field: field name to use as the condition key (default: "id")
    """
    command = monitor.extra.get("command")
    if not command:
        log.error(f"tool_poll monitor {monitor.name}: missing required 'command' param")
        return None

    id_field = monitor.extra.get("id_field", "id")
    # Split shell command string into list for subprocess
    try:
        cmd = shlex.split(command)
    except ValueError as e:
        log.error(f"tool_poll monitor {monitor.name}: bad command: {e}")
        return None

    env = dict(os.environ)
    return _run_command(cmd, env, TOOL_TIMEOUT, monitor.name, id_field)


# ---------------------------------------------------------------------------
# venn_poll — Venn-specific convenience
# ---------------------------------------------------------------------------

def _venn_binary() -> str | None:
    """Path to the venn CLI, or None when not installed."""
    return shutil.which("venn")


def venn_poll(monitor, projects: list[Path]) -> list[Condition] | None:
    """Venn-specific convenience: builds the venn CLI command from params.

    Required monitor.extra fields:
        service: Venn server ID (e.g. "work-gmail")
        tool:    Venn tool name (e.g. "list_messages")
    Optional:
        query:    JSON string of tool arguments (e.g. '{"maxResults": 5}')
        id_field: field name to use as the condition key (default: "id")
    """
    service = monitor.extra.get("service")
    tool = monitor.extra.get("tool")
    if not service or not tool:
        log.error(f"venn_poll monitor {monitor.name}: missing required "
                  f"'service' or 'tool' param")
        return None

    query = monitor.extra.get("query", "")
    id_field = monitor.extra.get("id_field", "id")

    binary = _venn_binary()
    if not binary:
        log.error(f"venn_poll monitor {monitor.name}: venn CLI not installed")
        return None

    cmd = [binary, "tools", "execute", "-s", service, "-t", tool]
    if query:
        cmd.extend(["-a", query])

    env = dict(os.environ)
    api_key = env.get("VENN_API_KEY", "")
    if api_key:
        env["VENN_API_KEY"] = api_key

    return _run_command(cmd, env, TOOL_TIMEOUT, monitor.name, id_field)


# Native check runners, keyed by the monitor's `check` field.
CHECKS = {
    "tool_poll": tool_poll,
    "venn_poll": venn_poll,
}
