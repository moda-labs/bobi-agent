"""Monitor scheduler — runs monitors on their intervals and injects events.

Runs as a background thread inside the manager process (alongside the event
drain loop). Every tick it reloads the registry (so monitors added at runtime
take effect without a restart), runs any monitor that's due, deduplicates the
detected conditions against persisted state, and injects a synthetic event
into the manager's event stream for each newly-appeared condition.

Synthetic events are pushed onto the same `event_queue` webhooks use, so the
manager receives and routes them exactly like a real webhook event.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .checks import CHECKS, _parse_iso
from .registry import MonitorRegistry

log = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".modastack" / "monitor_state.json"
TICK_INTERVAL = 30  # seconds between scheduler ticks


def _default_inject(event: dict) -> None:
    """Push a synthetic event onto the webhook event queue."""
    from modastack.manager.events.event_client import event_queue
    event_queue.put(event)


class MonitorScheduler:
    def __init__(self, inject_event=None, state_path: Path = STATE_PATH,
                 now=None, registry_loader=None):
        self.inject_event = inject_event or _default_inject
        self.state_path = Path(state_path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._registry_loader = registry_loader or MonitorRegistry.load
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
            check = CHECKS.get(monitor.check)
            if check is None:
                log.warning(f"Monitor {monitor.name} names unknown check "
                            f"'{monitor.check}' — skipping")
            else:
                try:
                    conditions = check(monitor, registry.repos_for(monitor))
                    self._reconcile(monitor, conditions)
                except Exception as e:
                    log.error(f"Check '{monitor.check}' for {monitor.name} failed: {e}")
        else:
            self._manager_interpreted(monitor)

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

    def _manager_interpreted(self, monitor) -> None:
        """No native check — ask the manager to interpret the description.

        Description-driven monitors can't be deduplicated by the scheduler
        (it never learns what the manager found), so this injects a
        check-due event each interval and relies on the manager to perform
        the check and decide whether to act.
        """
        event = {
            "type": "monitor.check_due",
            "source": "monitor",
            "data": {
                "monitor": monitor.name,
                "description": monitor.description,
                "event": monitor.event,
                **monitor.extra,
            },
        }
        log.info(f"Monitor {monitor.name} due (manager-interpreted)")
        self.inject_event(event)

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
