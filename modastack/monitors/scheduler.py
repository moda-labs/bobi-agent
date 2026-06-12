"""Monitor scheduler — runs monitors on their intervals and publishes findings.

Runs as a background thread inside the manager process (alongside the event
drain loop). Every tick it reloads the registry (so monitors added at runtime
take effect without a restart) and runs any monitor that's due.

Every monitor flavor is just a condition detector; what happens to detected
conditions is one shared path:

    detect (notify | command | native check | check agent)
      -> conditions
      -> _reconcile: dedup against persisted state
      -> _fire: publish to the event server
      -> subscribers (the manager included) receive it like any other event

Nothing is injected in-process. Findings travel through the event server's
topic routing, so they reach every subscriber, appear in events.jsonl, and
get seq/replay durability — identically for all flavors.

Detectors return a list of conditions, or None when the detection itself
failed (command error, check exception, no verdict). None is indeterminate:
state is left untouched — active conditions are not cleared and nothing
fires — and the next interval retries. An empty list means "all clear" and
clears active conditions.

A condition is recorded active only after its event actually publishes, so a
failed publish (event server briefly down) is retried on the next interval
instead of being lost.

Monitors run on an `interval` (e.g. '15m') or at wall-clock times
(`at: ["06:00", "18:00"]`, optionally pinned to a timezone with
`tz: America/Los_Angeles`). At-monitors don't fire on first sight — the
first tick records a baseline, then each scheduled time fires once.

Monitor flavors:
  - Notification (`notify: true`) — detects a single condition keyed to the
    due time, so dedup never suppresses it. For scheduled nudges like a
    twice-daily status roundup.
  - Command (`command:`) — runs a shell command and parses its JSON output
    into conditions.
  - Native check (`check:` field) — a deterministic Python runner in
    checks.py that the scheduler calls directly.
  - Description-only — the scheduler launches a short-lived, non-interactive
    check agent out-of-band (via `modastack agents launch`), captures its
    verdict, and converts it into conditions. The check agent only observes
    and reports — dedup and publishing happen here, on the same path as
    every other flavor. The manager never sees the check process itself.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


def _load_checks(project_path: Path | None = None) -> dict:
    """Load check runners from installed monitors/*_checks.py files."""
    all_checks: dict = {}
    if not project_path:
        from modastack.sdk import get_project_root
        project_path = get_project_root()
    if not project_path:
        return all_checks
    from modastack import paths
    checks_dir = paths.monitors_dir(project_path)
    if not checks_dir.exists():
        return all_checks
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
from .schema import Condition

log = logging.getLogger(__name__)

def _monitor_state_path() -> Path:
    from modastack import paths
    return paths.state_dir() / "monitor_state.json"


TICK_INTERVAL = 30  # seconds between scheduler ticks


def _default_publish(event: str, data: dict) -> bool:
    """Publish a monitor finding to the event server.

    Returns True when the server accepted it. The same wire path every
    out-of-band agent uses — there is no in-process shortcut.
    """
    from modastack.events.publish import post_event
    return post_event(event, data)


def _parse_verdict(output: str) -> dict | None:
    """Extract the trailing verdict JSON a check process printed, or None.

    `modastack agents launch --wait` prints the check's verdict as a single
    JSON line ({"success": ..., "finding": ...}). None means the process
    produced no parseable verdict — an indeterminate run, never "all clear".
    """
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "finding" in parsed:
            return parsed
    return None


def _default_spawn_check(monitor, cwd: str | None, on_verdict) -> None:
    """Launch a non-interactive check subprocess and report its verdict.

    Runs `modastack agents launch --wait` out-of-band so the scheduler thread
    is never blocked, captures the check's stdout, and hands the trailing
    verdict JSON (or None) to ``on_verdict`` from a waiter thread when the
    process exits. The check agent only observes — converting the verdict to
    conditions, dedup, and publishing all happen in the scheduler.
    """
    role = getattr(monitor, "role", "") or ""
    if not role:
        try:
            from modastack.config import Config
            from modastack.sdk import get_project_root
            root = get_project_root()
            if root:
                cfg = Config.load(root)
                role = cfg.entry_point
        except Exception:
            pass

    cmd = [
        sys.executable, "-m", "modastack.cli",
        "agents", "launch",
        "-w", "adhoc",
        *(["--role", role] if role else []),
        "--non-interactive",
        "--wait",
        "--task", monitor.description or monitor.name,
    ]

    from modastack import paths
    log_path = paths.state_dir() / "manager.log"

    try:
        with open(log_path, "a") as lf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=lf,
                                    text=True, start_new_session=True)
    except OSError as e:
        log.error(f"Failed to spawn check for monitor {monitor.name}: {e}")
        return

    def _wait() -> None:
        from modastack.subagent import CHECK_TIMEOUT
        # run_check_blocking retries internally (attempts=2, each bounded by
        # CHECK_TIMEOUT), so allow both attempts plus startup slack.
        budget = 2 * CHECK_TIMEOUT + 120
        try:
            out, _ = proc.communicate(timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            log.error(f"Check for monitor {monitor.name} exceeded {budget}s — killed")
            on_verdict(None)
            return
        if out:  # keep the check's output observable in manager.log
            try:
                with open(log_path, "a") as lf:
                    lf.write(out)
            except OSError:
                pass
        on_verdict(_parse_verdict(out))

    threading.Thread(target=_wait, daemon=True,
                     name=f"check-wait-{monitor.name}").start()


class MonitorScheduler:
    def __init__(self, publish=None, state_path: Path | None = None,
                 now=None, registry_loader=None, spawn_check=None,
                 project_path: Path | None = None):
        self.publish = publish or _default_publish
        self.spawn_check = spawn_check or _default_spawn_check
        self.state_path = Path(state_path) if state_path else _monitor_state_path()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._project_path = project_path
        self._checks = _load_checks(project_path)
        self._registry_loader = registry_loader or (
            lambda **kw: MonitorRegistry.load(project_path=project_path, **kw)
        )
        self.state: dict = self._load_state()
        # Waiter threads for out-of-band checks reconcile concurrently with
        # the scheduler thread — all state mutation goes through this lock.
        self._state_lock = threading.RLock()
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
        if monitor.at:
            return self._due_at(monitor, now, last_run)
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

    def _due_at(self, monitor, now: datetime, last_run: str | None) -> bool:
        """Due when a scheduled wall-clock time has passed since the last run.

        Unlike interval monitors, an at-monitor does NOT fire on first sight —
        starting the manager at 2pm shouldn't trigger the 6am slot. The first
        tick just records a baseline; subsequent ticks fire once per scheduled
        time crossed.
        """
        try:
            scheduled = self._last_scheduled(monitor, now)
        except ValueError as e:
            log.warning(f"Monitor {monitor.name} has bad at-times: {e}")
            return False
        last = _parse_iso(last_run) if last_run else None
        if last is None:
            with self._state_lock:
                self.state.setdefault(monitor.state_key, {})["last_run"] = now.isoformat()
                self._save_state()
            return False
        return scheduled > last

    @staticmethod
    def _last_scheduled(monitor, now: datetime) -> datetime:
        """The most recent scheduled fire time at or before `now`, computed in
        the monitor's timezone (so '06:00' means 6am in `tz`, not UTC)."""
        from datetime import timedelta

        local = now.astimezone(monitor.tzinfo)
        candidates = []
        for hour, minute in monitor.at_times:
            t = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t > local:
                t -= timedelta(days=1)
            candidates.append(t)
        return max(candidates)

    def run_monitor(self, monitor, registry: MonitorRegistry, now: datetime) -> None:
        """Detect conditions for one monitor and reconcile them.

        Detection is the only flavor-specific step. Description-only checks
        detect out-of-band: nothing reconciles here, the waiter thread calls
        back into _reconcile when the verdict lands.
        """
        if monitor.notify:
            # Scheduled notification — keyed to the due time, so the shared
            # dedup path never suppresses it.
            conditions: list | None = [
                Condition(key=now.isoformat(),
                          data={"description": monitor.description})
            ]
        elif monitor.command:
            conditions = self._command_conditions(monitor)
        elif monitor.check:
            conditions = self._check_conditions(monitor, registry)
        else:
            self._spawn_check(monitor, registry.projects_for(monitor))
            conditions = None  # detection in flight — reconciled on verdict

        if conditions is not None:
            self._reconcile(monitor, conditions)

        with self._state_lock:
            self.state.setdefault(monitor.state_key, {})["last_run"] = now.isoformat()
            self._save_state()

    def _reconcile(self, monitor, conditions: list) -> None:
        """The single dedup + publish chokepoint for every monitor flavor.

        Fires events only for conditions that weren't active last time.
        Conditions that disappeared drop out; if they recur later they fire
        again. A still-present condition is never re-fired on the next
        interval. A new condition is recorded active only once its event
        actually published — a failed publish retries next interval.
        """
        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            previous = set(entry.get("active", []))
            current = {c.key: c for c in conditions}
            active: list[str] = []
            for key, condition in current.items():
                if key in previous or self._fire(monitor, condition):
                    active.append(key)
            entry["active"] = active
            self._save_state()

    def _fire(self, monitor, condition) -> bool:
        event = monitor.event or f"monitor/{monitor.name}"
        ok = self.publish(event, {"monitor": monitor.name, **condition.data})
        if ok:
            log.info(f"Monitor {monitor.name} fired {event} ({condition.key})")
        else:
            log.warning(f"Monitor {monitor.name} failed to publish {event} "
                        f"({condition.key}) — will retry next interval")
        return ok

    # --- detectors ------------------------------------------------------

    def _check_conditions(self, monitor, registry: MonitorRegistry) -> list | None:
        """Run a native check runner. None when the check itself failed."""
        check = self._checks.get(monitor.check)
        if check is None:
            log.warning(f"Monitor {monitor.name} names unknown check "
                        f"'{monitor.check}' — skipping")
            return None
        try:
            return check(monitor, registry.projects_for(monitor))
        except Exception as e:
            log.error(f"Check '{monitor.check}' for {monitor.name} failed: {e}")
            return None

    def _command_conditions(self, monitor) -> list | None:
        """Run a shell command and parse its JSON output into conditions.

        None when the command failed or printed garbage (indeterminate);
        an empty list when it succeeded with no output (all clear).
        """
        try:
            result = subprocess.run(
                monitor.command, shell=True, capture_output=True, text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            log.error(f"Command monitor {monitor.name} timed out")
            return None
        except OSError as e:
            log.error(f"Command monitor {monitor.name} failed to run: {e}")
            return None

        if result.returncode != 0:
            stderr = result.stderr.strip()[:200] if result.stderr else ""
            log.warning(f"Command monitor {monitor.name} exited {result.returncode}: {stderr}")
            return None

        stdout = result.stdout.strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            log.warning(f"Command monitor {monitor.name} returned non-JSON output")
            return None

        items = data if isinstance(data, list) else [data]
        conditions = []
        for item in items:
            if isinstance(item, dict):
                raw_id = item.get("id")
                key = str(raw_id) if raw_id is not None else hashlib.sha256(
                    json.dumps(item, sort_keys=True).encode()
                ).hexdigest()[:12]
                conditions.append(Condition(key=str(key), data=item))
        return conditions

    def _spawn_check(self, monitor, projects: list[Path]) -> None:
        """No native check — run the description as an out-of-band detector.

        Rather than injecting a check-due event into the manager (which would
        pollute its context and tie it up every interval), the scheduler
        launches a short-lived, non-interactive check agent. The waiter thread
        hands its verdict to _on_check_verdict, which reconciles through the
        same path as every other flavor — the manager only ever sees an
        actionable finding, never the check itself.

        The check just needs a working directory to run read-only commands in;
        it runs once in the first applicable project (or no project for a pure
        URL/API check).
        """
        cwd = str(projects[0]) if projects else None
        log.info(f"Monitor {monitor.name} due — spawning non-interactive check")
        self.spawn_check(monitor, cwd,
                         lambda verdict: self._on_check_verdict(monitor, verdict))

    def _on_check_verdict(self, monitor, verdict: dict | None) -> None:
        """Reconcile an out-of-band check's verdict (waiter-thread callback)."""
        conditions = self._verdict_conditions(verdict)
        if conditions is None:
            log.warning(f"Monitor {monitor.name}: check indeterminate — "
                        "leaving state untouched, retrying next interval")
            return
        self._reconcile(monitor, conditions)

    @staticmethod
    def _verdict_conditions(verdict: dict | None) -> list | None:
        """Convert a check verdict into conditions for the shared path.

        None / success=false is indeterminate. An explicit finding=false is
        all clear. A finding keys on details.key (or details.id) when the
        check supplied one, else a hash of the summary — so the same condition
        observed by successive checks dedups exactly like a native check's.
        """
        if not isinstance(verdict, dict) or not verdict.get("success", False):
            return None
        if not verdict.get("finding"):
            return []
        details = verdict.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        summary = str(verdict.get("summary", ""))
        key = str(details.get("key") or details.get("id") or "")
        if not key:
            key = hashlib.sha256(summary.encode()).hexdigest()[:12]
        return [Condition(key=key, data={"summary": summary, "text": summary,
                                         **details})]

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
