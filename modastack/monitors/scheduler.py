"""Monitor scheduler — runs monitors on their intervals and injects events.

Runs as a background thread inside the manager process (alongside the event
drain loop). Every tick it reloads the registry (so monitors added at runtime
take effect without a restart), runs any monitor that's due, deduplicates the
detected conditions against persisted state, and injects a synthetic event
into the manager's event stream for each newly-appeared condition.

Synthetic events are pushed onto the same `event_queue` webhooks use, so the
manager receives and routes them exactly like a real webhook event.

Monitors come in two flavors:
  - Native check (`check:` field) — a deterministic Python runner in
    checks.py that the scheduler calls directly and deduplicates.
  - Description-only — the scheduler launches a short-lived, non-interactive
    check agent out-of-band (via `modastack spawn --non-interactive`), which
    performs the check and posts a result event back to the bus only if it
    finds something. The manager never sees the check process itself — just
    the eventual finding — so its context stays clean and responsive.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import importlib.util
import sys

def _load_checks(agent_name: str | None = None) -> dict:
    """Load check runners from agent pack monitors/*_checks.py files."""
    all_checks: dict = {}
    search_dirs: list[Path] = []
    if agent_name:
        from modastack.prompts.resolver import _resolve_agent_dir
        agent_dir = _resolve_agent_dir(agent_name)
        if agent_dir:
            search_dirs.append(agent_dir / "monitors")
    for checks_dir in search_dirs:
        if not checks_dir.exists():
            continue
        for py_file in checks_dir.glob("*_checks.py"):
            module_name = f"modastack_checks.{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
                if hasattr(mod, "CHECKS"):
                    all_checks.update(mod.CHECKS)
    return all_checks

def _parse_iso(value: str):
    from datetime import datetime as _dt, timezone as _tz
    if not value:
        return None
    try:
        dt = _dt.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt
from .registry import MonitorRegistry

log = logging.getLogger(__name__)

def _monitor_state_path() -> Path:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        raise RuntimeError("project root not set — call set_project_root() first")
    d = root / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "monitor_state.json"
TICK_INTERVAL = 30  # seconds between scheduler ticks


def _default_inject(event: dict) -> None:
    """Push a synthetic event onto the webhook event queue."""
    from modastack.events.client import event_queue
    event_queue.put(event)


def _default_spawn_check(monitor, cwd: str | None) -> None:
    """Launch a non-interactive check as a background subprocess.

    Uses `modastack agents launch --wait --post-event` so the check runs
    out-of-band. Fire-and-forget: the scheduler thread is never blocked,
    and the subprocess posts a result event back to the bus only on a finding.
    """
    cmd = [
        sys.executable, "-m", "modastack.cli",
        "agents", "launch",
        "-w", "adhoc",
        "--role", "engineer",
        "--non-interactive",
        "--wait",
        "--task", monitor.description or monitor.name,
        "--post-event", monitor.event,
    ]

    try:
        from modastack.sdk import get_project_root
        root = get_project_root()
        if not root:
            raise RuntimeError("project root not set — call set_project_root() first")
        log_path = root / ".modastack" / "state" / "manager.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
    except OSError as e:
        log.error(f"Failed to spawn check for monitor {monitor.name}: {e}")


class MonitorScheduler:
    def __init__(self, inject_event=None, state_path: Path | None = None,
                 now=None, registry_loader=None, spawn_check=None,
                 agent_name: str | None = None):
        self.inject_event = inject_event or _default_inject
        self.spawn_check = spawn_check or _default_spawn_check
        self.state_path = Path(state_path) if state_path else _monitor_state_path()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._agent_name = agent_name
        self._checks = _load_checks(agent_name)
        self._registry_loader = registry_loader or (
            lambda **kw: MonitorRegistry.load(agent_name=agent_name, **kw)
        )
        self.state: dict = self._load_state()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="monitor-scheduler")
        self._thread.start()
        log.info("Monitor scheduler started")
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let one bad tick kill the loop
                log.error(f"Monitor scheduler tick failed: {e}")
            self._stop.wait(TICK_INTERVAL)

    # --- core logic ----------------------------------------------------

    def tick(self) -> None:
        """Run every monitor that is currently due."""
        registry = self._registry_loader()
        now = self._now()
        for monitor in registry.effective_monitors():
            if self._due(monitor, now):
                self.run_monitor(monitor, registry, now)

    def _due(self, monitor, now: datetime) -> bool:
        entry = self.state.get(monitor.state_key)
        last_run = entry.get("last_run") if entry else None
        if not last_run:
            return True  # never run -> run on startup
        last = _parse_iso(last_run)
        if last is None:
            return True
        try:
            return (now - last).total_seconds() >= monitor.interval_seconds
        except ValueError as e:
            log.warning(f"Monitor {monitor.name} has bad interval: {e}")
            return False

    def run_monitor(self, monitor, registry: MonitorRegistry, now: datetime) -> None:
        if monitor.check:
            check = self._checks.get(monitor.check)
            if check is None:
                log.warning(f"Monitor {monitor.name} names unknown check "
                            f"'{monitor.check}' — skipping")
            else:
                try:
                    conditions = check(monitor, registry.projects_for(monitor))
                    self._reconcile(monitor, conditions)
                except Exception as e:
                    log.error(f"Check '{monitor.check}' for {monitor.name} failed: {e}")
        else:
            self._spawn_check(monitor, registry.projects_for(monitor))

        self.state.setdefault(monitor.state_key, {})["last_run"] = now.isoformat()
        self._save_state()

    def _reconcile(self, monitor, conditions: list) -> None:
        """Fire events only for conditions that weren't active last time."""
        entry = self.state.setdefault(monitor.state_key, {})
        previous = set(entry.get("active", []))
        current = {c.key: c for c in conditions}
        for key, condition in current.items():
            if key not in previous:
                self._fire(monitor, condition)
        # Conditions that disappeared drop out; if they recur later they fire
        # again. This is the deduplication: a still-present condition is never
        # re-fired on the next interval.
        entry["active"] = list(current.keys())

    def _fire(self, monitor, condition) -> None:
        source, etype = monitor.event_parts
        event = {
            "type": etype,
            "source": source,
            "data": {"monitor": monitor.name, **condition.data},
        }
        log.info(f"Monitor {monitor.name} fired {monitor.event} ({condition.key})")
        self.inject_event(event)

    def _spawn_check(self, monitor, projects: list[Path]) -> None:
        """No native check — run the description as a non-interactive check.

        Rather than injecting a check-due event into the manager (which would
        pollute its context and tie it up every interval), the scheduler
        launches a short-lived, out-of-band check agent. That process performs
        the check from the monitor's description and posts a result event back
        to the bus only if it finds something — so the manager only ever sees
        an actionable finding, never the check itself.

        The check just needs a working directory to run read-only commands in;
        it runs once in the first applicable project (or no project for a pure
        URL/API check).
        """
        cwd = str(projects[0]) if projects else None
        log.info(f"Monitor {monitor.name} due — spawning non-interactive check")
        self.spawn_check(monitor, cwd)

    # --- state persistence ---------------------------------------------

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            log.warning(f"Corrupt monitor state at {self.state_path} — resetting")
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))
        except OSError as e:
            log.warning(f"Failed to persist monitor state: {e}")
