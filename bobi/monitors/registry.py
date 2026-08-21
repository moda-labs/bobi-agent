"""Monitor registry — merge installed defaults with package overrides.

Monitors resolve exclusively from the installed pack image:

    run/package/monitors/defaults.yaml  →  run/package/monitors.yaml / agent.yaml

A package entry with `enabled: false` disables a default monitor.
A package entry that shares a name with a default monitor overrides it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from bobi import paths

from .schema import Monitor

log = logging.getLogger(__name__)

def _defaults_path(project_path: Path | None = None) -> Path | None:
    """Return the installed monitor defaults path.

    Only reads from run/package/monitors/defaults.yaml. Returns None when no
    runtime is selected.
    """
    if not project_path:
        from bobi.paths import bound_root as get_project_root
        project_path = get_project_root()
    if not project_path:
        return None
    return paths.monitors_dir(project_path) / "defaults.yaml"


def _read_records(path: Path | None) -> tuple[list[dict], bool]:
    """Read the `monitors:` list from a YAML file, tolerating absence.

    Returns the records and whether the read was COMPLETE. An absent file is
    complete (nothing was configured there); a file that would not parse is
    not, and neither is a `monitors:` list carrying entries that are not
    records. The distinction matters to anything reasoning about what is
    missing from the registry: a monitor absent because it was deleted and a
    monitor absent because its file has an unclosed bracket look identical
    otherwise, and they call for opposite behaviour.
    """
    if path is None or not path.exists():
        return [], True
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning(f"Failed to parse monitors from {path}: {e}")
        return [], False
    except OSError as e:
        log.warning(f"Failed to read monitors from {path}: {e}")
        return [], False
    records = raw.get("monitors") or []
    kept = [r for r in records if isinstance(r, dict)]
    return kept, len(kept) == len(records)


class MonitorRegistry:
    """The merged, resolved view of all monitors across the three tiers."""

    def __init__(self, project_path: Path | None = None):
        self.project_path = project_path
        self.globals: dict[str, Monitor] = {}
        self.project_monitors: list[Monitor] = []
        self.opt_outs: dict[str, set[str]] = {}
        # Whether every monitor that is configured actually made it in. False
        # when a file would not parse or a record was skipped: the registry
        # degrades one file or one record at a time and never fails totally,
        # so "no monitors" is not the shape a broken config takes.
        self.load_complete = True

    @classmethod
    def load(cls, project_path: Path | None = None) -> "MonitorRegistry":
        registry = cls(project_path=project_path)
        registry._load()
        return registry

    def _load(self) -> None:
        defaults, complete = _read_records(_defaults_path(self.project_path))
        self.load_complete = self.load_complete and complete
        for raw in defaults:
            try:
                m = Monitor.from_dict(raw, source="default")
                self.globals[m.name] = m
            except ValueError as e:
                log.warning(f"Skipping bad default monitor: {e}")
                self.load_complete = False

        # 2. Project-specific monitors
        project_paths = [self.project_path] if self.project_path else []
        for project_path in project_paths:
            project_key = str(project_path)
            project_sources = [
                paths.package_dir(project_path) / "monitors.yaml",
                paths.agent_yaml_path(project_path),
            ]
            for config_path in project_sources:
                records, complete = _read_records(config_path)
                self.load_complete = self.load_complete and complete
                for raw in records:
                    try:
                        m = Monitor.from_dict(raw, source=project_key, project=project_key)
                    except ValueError as e:
                        log.warning(f"Skipping bad monitor in {config_path}: {e}")
                        self.load_complete = False
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
    def add_project(monitor: Monitor, project_path: Path) -> None:
        """Append or replace a monitor in run/package/monitors.yaml."""
        monitors_path = paths.package_dir(project_path) / "monitors.yaml"
        monitors_path.parent.mkdir(parents=True, exist_ok=True)
        records, _complete = _read_records(monitors_path)
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
        from bobi.paths import bound_root as get_project_root
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
            monitors_path = paths.package_dir(project_path) / "monitors.yaml"
            records, _complete = _read_records(monitors_path)
            kept = [r for r in records if r.get("name") != name]
            if len(kept) == len(records):
                return "not-found"
            monitors_path.write_text(
                yaml.dump({"monitors": kept}, default_flow_style=False, sort_keys=False)
            )
            return "removed"

        # Present only as a built-in default — can't delete, must pause.
        defaults, _complete = _read_records(_defaults_path())
        for raw in defaults:
            if raw.get("name") == name:
                return "default-only"
        return "not-found"
