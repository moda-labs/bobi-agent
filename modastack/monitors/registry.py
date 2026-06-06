"""Monitor registry — merge the three storage tiers into one view.

Load order (most general to most specific), later tiers override by `name`:

    built-in defaults  ->  user globals  ->  project-specific

A project-specific entry with `enabled: false` opts that project out of an
inherited monitor. A project-specific entry that shares a name with a global
monitor overrides it for that project (the global one skips that project).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .schema import Monitor

log = logging.getLogger(__name__)

DEFAULTS_PATH = Path(__file__).resolve().parent / "defaults.yaml"


def _read_records(path: Path) -> list[dict]:
    """Read the `monitors:` list from a YAML file, tolerating absence."""
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning(f"Failed to parse monitors from {path}: {e}")
        return []
    records = raw.get("monitors") or []
    return [r for r in records if isinstance(r, dict)]


class MonitorRegistry:
    """The merged, resolved view of all monitors across the three tiers."""

    def __init__(self, project_path: Path | None = None):
        self.project_path = project_path
        self.globals: dict[str, Monitor] = {}
        self.project_monitors: list[Monitor] = []
        self.opt_outs: dict[str, set[str]] = {}

    @classmethod
    def load(cls, project_path: Path | None = None) -> "MonitorRegistry":
        registry = cls(project_path=project_path)
        registry._load()
        return registry

    def _load(self) -> None:
        # 1. Built-in defaults
        for raw in _read_records(DEFAULTS_PATH):
            try:
                m = Monitor.from_dict(raw, source="default")
                self.globals[m.name] = m
            except ValueError as e:
                log.warning(f"Skipping bad default monitor: {e}")

        # 2. Project-specific monitors
        project_paths = [self.project_path] if self.project_path else []
        for project_path in project_paths:
            project_key = str(project_path)
            project_sources = [
                project_path / ".modastack" / "monitors.yaml",
                project_path / ".modastack" / "config.yaml",
            ]
            for config_path in project_sources:
                for raw in _read_records(config_path):
                    try:
                        m = Monitor.from_dict(raw, source=project_key, project=project_key)
                    except ValueError as e:
                        log.warning(f"Skipping bad monitor in {config_path}: {e}")
                        continue
                    if not m.enabled:
                        self.opt_outs.setdefault(m.name, set()).add(project_key)
                        continue
                    self.project_monitors.append(m)
                    if m.name in self.globals:
                        self.opt_outs.setdefault(m.name, set()).add(project_key)

    def effective_monitors(self) -> list[Monitor]:
        """All enabled monitors that should actually be scheduled."""
        result = [m for m in self.globals.values() if m.enabled]
        result.extend(self.project_monitors)
        return result

    def all_monitors(self) -> list[Monitor]:
        """Every resolved monitor, including paused (enabled: false) ones."""
        return list(self.globals.values()) + self.project_monitors

    def projects_for(self, monitor: Monitor) -> list[Path]:
        """Which projects a monitor's check should run against.

        Project-scoped monitors run only on their project; global monitors run on
        every registered project except those that opted out or overrode them.
        """
        if monitor.project:
            return [Path(monitor.project)]
        if not self.project_path:
            return []
        opted_out = self.opt_outs.get(monitor.name, set())
        if str(self.project_path) in opted_out:
            return []
        return [self.project_path]

    # --- Writes to user-writable tiers ---------------------------------

    @staticmethod
    def add_global(monitor: Monitor) -> None:
        """Deprecated — use add_project instead. Global monitors are not supported."""
        raise NotImplementedError(
            "Global monitors removed. Use `modastack monitors add --project .` instead."
        )

    @staticmethod
    def add_project(monitor: Monitor, project_path: Path) -> None:
        """Append or replace a monitor in .modastack/monitors.yaml."""
        monitors_path = project_path / ".modastack" / "monitors.yaml"
        monitors_path.parent.mkdir(parents=True, exist_ok=True)
        records = _read_records(monitors_path)
        records = [r for r in records if r.get("name") != monitor.name]
        records.append(monitor.to_dict())
        monitors_path.write_text(
            yaml.dump({"monitors": records}, default_flow_style=False, sort_keys=False)
        )

    @classmethod
    def pause(cls, name: str, project_path: Path | None = None) -> bool:
        """Disable a monitor by writing `enabled: false` to a writable tier.

        Works for built-in defaults too — the override lands in user globals
        (or the given project's config) and wins by load order.
        """
        registry = cls.load()
        existing = registry.globals.get(name)
        if project_path is not None:
            base = existing or Monitor(name=name)
            base.enabled = False
            cls.add_project(base, project_path)
            return True
        if existing is None:
            return False
        from modastack.sdk import get_project_root
        rp = get_project_root()
        if rp:
            existing.enabled = False
            cls.add_project(existing, rp)
            return True
        return False

    @classmethod
    def remove(cls, name: str, project_path: Path | None = None) -> str:
        """Remove a monitor from a user-writable tier.

        Returns: "removed", "default-only" (can't delete a built-in — pause
        it instead), or "not-found".
        """
        if project_path is not None:
            monitors_path = project_path / ".modastack" / "monitors.yaml"
            records = _read_records(monitors_path)
            kept = [r for r in records if r.get("name") != name]
            if len(kept) == len(records):
                return "not-found"
            monitors_path.write_text(
                yaml.dump({"monitors": kept}, default_flow_style=False, sort_keys=False)
            )
            return "removed"

        # Present only as a built-in default — can't delete, must pause.
        for raw in _read_records(DEFAULTS_PATH):
            if raw.get("name") == name:
                return "default-only"
        return "not-found"
