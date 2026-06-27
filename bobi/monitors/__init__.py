"""Background monitoring tasks — scheduled polling to fill webhook gaps.

Webhooks don't cover every state change the manager cares about (merge
conflicts, stale PRs, deploy health, engineer stalls). Monitors are
human-readable polling tasks that run on an interval, detect a condition,
and inject a synthetic event into the manager's event stream — exactly
like a webhook would.

Two storage tiers, merged most-general to most-specific (later tiers
override earlier ones by `name`):

  1. Built-in defaults  — agent team monitors/defaults.yaml (shipped, read-only)
  2. Package-specific   — <run>/package/monitors.yaml
"""

from pathlib import Path

from .schema import Monitor, parse_interval
from .registry import MonitorRegistry

# Framework-default monitors (#471), seeded into every composed team image as the
# most-base layer of the `from:` chain — see compose._seed_framework_monitors.
# Team-overridable (a same-named record wins) and prunable (opt-out). Ships as
# package data the same way prompts/curator.md does.
FRAMEWORK_DEFAULTS_PATH = Path(__file__).parent / "framework_defaults.yaml"

__all__ = ["Monitor", "parse_interval", "MonitorRegistry", "FRAMEWORK_DEFAULTS_PATH"]
