"""Background monitoring tasks — scheduled polling to fill webhook gaps.

Webhooks don't cover every state change the manager cares about (merge
conflicts, stale PRs, deploy health, engineer stalls). Monitors are
human-readable polling tasks that run on an interval, detect a condition,
and inject a synthetic event into the manager's event stream — exactly
like a webhook would.

Three storage tiers, merged most-general to most-specific (later tiers
override earlier ones by `name`):

  1. Built-in defaults  — monitors/defaults.yaml (shipped, read-only)
  2. User globals       — ~/.modastack/monitors.yaml (apply to all repos)
  3. Repo-specific      — <repo>/.modastack.yaml under `monitors:`
"""

from .schema import Monitor, parse_interval
from .registry import MonitorRegistry

__all__ = ["Monitor", "parse_interval", "MonitorRegistry"]
