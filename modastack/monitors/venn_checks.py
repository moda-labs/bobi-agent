"""Venn CLI poll check — $0 LLM native check runner.

Pulls items from a Venn-connected service via the CLI, normalizes the
result to {id, ...} conditions, and returns them for the scheduler's
_reconcile path. Dedup/new-item semantics come for free from _reconcile.

This is a framework-level check (not pack-level) so any team can use it.
Monitor params come from extra fields: service, tool, query, id_field.

Example monitor YAML:
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
import shutil
import subprocess
from pathlib import Path

from modastack.monitors.schema import Condition

log = logging.getLogger(__name__)

VENN_TIMEOUT = 60


def _venn_binary() -> str | None:
    """Path to the venn CLI, or None when not installed."""
    return shutil.which("venn")


def venn_poll(monitor, projects: list[Path]) -> list[Condition] | None:
    """Pull items from a Venn service and return them as conditions.

    Returns a list of Condition objects (possibly empty = all clear),
    or None when the pull itself failed (indeterminate — state untouched,
    retried next interval).

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

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=VENN_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        log.error(f"venn_poll monitor {monitor.name}: timed out after {VENN_TIMEOUT}s")
        return None
    except OSError as e:
        log.error(f"venn_poll monitor {monitor.name}: failed to run: {e}")
        return None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        log.warning(f"venn_poll monitor {monitor.name}: exit {result.returncode}: {stderr}")
        return None

    stdout = result.stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning(f"venn_poll monitor {monitor.name}: non-JSON output")
        return None

    # Venn CLI wraps results in {"result": [...]} or returns a plain list
    if isinstance(data, dict):
        items = data.get("result", [])
        if not isinstance(items, list):
            items = [items] if items else []
    elif isinstance(data, list):
        items = data
    else:
        items = []

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


# Native check runners, keyed by the monitor's `check` field.
CHECKS = {
    "venn_poll": venn_poll,
}
