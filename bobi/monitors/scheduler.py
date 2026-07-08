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
`tz: America/Los_Angeles`, and optionally gated to specific weekdays with
`days: [sun]` for weekly recurrence). At-monitors don't fire on first sight —
the first tick records a baseline, then each scheduled time fires once. A
plain daily `at:` slot missed during downtime fires once, late (catch-up); a
weekday-gated weekly slot does not catch up — a missed run is skipped.

Monitor flavors:
  - Notification (`notify: true`) — detects a single condition keyed to the
    due time, so dedup never suppresses it. For scheduled nudges like a
    twice-daily status roundup.
  - Command (`command:`) — runs a shell command and parses its JSON output
    into conditions.
  - Native check (`check:` field) — a deterministic Python runner in
    checks.py that the scheduler calls directly.
  - Description-only — the scheduler launches a short-lived, non-interactive
    check agent out-of-band (via `bobi agent <name> subagents launch`), captures its
    verdict, and converts it into conditions. The check agent only observes
    and reports — dedup and publishing happen here, on the same path as
    every other flavor. The manager never sees the check process itself.
  - Curator (`curator: true`) — the one flavor whose agent WRITES an artifact
    (policy.md) instead of returning a verdict (#456). The scheduler does the
    deterministic half (window the transcript delta on messages.id since a
    success-advanced cursor, apply the per-run input cap), launches the curator
    agent with the rendered delta, and on a successful run advances the cursor
    and publishes `system/policy.updated` directly — bypassing _reconcile dedup
    because a completion signal is not a deduped finding.

A command/check monitor may additionally set `relevance:` (#630) - the
two-tier semantic gate. The mechanical detector still decides what exists at
$0; the scheduler dedups first and sends ONLY the new conditions to a
short-lived cheap-model gate agent that judges them against the criterion.
Relevant items publish normally; irrelevant items are recorded active without
publishing, so each item is judged exactly once. A tick with nothing new
never touches a model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import threading
import tempfile
import time as time_mod
from datetime import datetime, timezone
from pathlib import Path


def _load_framework_checks() -> dict:
    """Load check runners bundled with the framework (bobi/monitors/*_checks.py)."""
    checks: dict = {}
    framework_dir = Path(__file__).parent
    for py_file in framework_dir.glob("*_checks.py"):
        module_name = f"bobi.monitors.{py_file.stem}"
        if module_name in sys.modules:
            mod = sys.modules[module_name]
        else:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
            else:
                continue
        if hasattr(mod, "CHECKS"):
            checks.update(mod.CHECKS)
    return checks


def _load_checks(project_path: Path | None = None) -> dict:
    """Load check runners from framework and installed monitors/*_checks.py files.

    Framework-level checks (bobi/monitors/*_checks.py) load first,
    then pack-level checks from the project's installed monitors directory
    can override or extend them.
    """
    all_checks: dict = _load_framework_checks()
    if not project_path:
        from bobi.paths import bound_root as get_project_root
        project_path = get_project_root()
    if not project_path:
        return all_checks
    from bobi import paths
    checks_dir = paths.monitors_dir(project_path)
    if not checks_dir.exists():
        return all_checks
    for py_file in checks_dir.glob("*_checks.py"):
        module_name = f"bobi_checks.{py_file.stem}"
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
    from bobi import paths
    return paths.state_dir() / "monitor_state.json"


TICK_INTERVAL = 30  # seconds between scheduler ticks

# Most new items a single relevance-gate call judges (#630). Overflow items
# are simply not recorded, so the next tick re-detects them as new and gates
# the next batch - bounded prompt size. Assumes the detector re-reports
# pending items (poll windows must cover a burst across intervals).
GATE_MAX_ITEMS = 20

# Linux MAX_ARG_STRLEN is 131072 bytes; keep monitor agent argv elements well
# below that so failures are caught before Popen raises E2BIG.
MAX_MONITOR_ARG_BYTES = 100_000

# How late a weekday-gated (`days:`) at-monitor may fire and still count as a
# live run rather than a missed-while-down catch-up. A live fire lands within
# one tick of the scheduled instant; anything later means the manager was down
# across it, so the weekly run is skipped (D8 — no catch-up). Two ticks of
# slack absorb tick jitter without ever catching up a real outage.
_AT_CATCHUP_GRACE = 2 * TICK_INTERVAL


def _default_publish(event: str, data: dict) -> bool:
    """Publish a monitor finding to the event server.

    Returns True when the server accepted it. The same wire path every
    out-of-band agent uses — there is no in-process shortcut.
    """
    from bobi.events.publish import post_event
    return post_event(event, data)


def _append_manager_log(message: str) -> None:
    try:
        from bobi import paths
        log_path = paths.state_dir() / "manager.log"
        with open(log_path, "a") as lf:
            lf.write(message.rstrip() + "\n")
    except Exception:
        pass


def _publish_monitor_error(monitor_name: str, kind: str, reason: str,
                           detail: str = "", publish=None) -> None:
    payload = {
        "monitor": monitor_name,
        "flavor": kind,
        "reason": reason,
        "detail": detail,
    }
    publisher = publish or _default_publish
    try:
        publisher("system/monitor.error", payload)
    except Exception:
        log.exception("Failed to publish monitor.error for %s", monitor_name)


def _resolve_monitor_role(monitor) -> str:
    """Resolve the role for a monitor-launched subagent."""
    role = getattr(monitor, "role", "") or ""
    if role:
        return role
    try:
        from bobi.config import Config
        from bobi.paths import bound_root as get_project_root
        root = get_project_root()
        if root:
            cfg = Config.load(root)
            return cfg.entry_point or "manager"
    except Exception:
        pass
    return ""


def _parse_stdout_json(output: str, key: str) -> dict | None:
    """Extract the trailing JSON line containing ``key`` that a monitor
    subprocess printed, or None.

    The shared stdout parser for every out-of-band monitor agent. None means
    the process produced no parseable verdict - an indeterminate run, never
    "all clear".
    """
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and key in parsed:
            return parsed
    return None


def _parse_verdict(output: str) -> dict | None:
    """The check flavor's verdict line: {"success": ..., "finding": ...}."""
    return _parse_stdout_json(output, "finding")


def _parse_gate_output(output: str) -> dict | None:
    """The gate flavor's verdict line: {"success": ..., "relevant": [...]}."""
    return _parse_stdout_json(output, "relevant")


def _spawn_monitor_agent(cmd, monitor_name: str, kind: str, parse,
                         on_result, cleanup=None, publish=None) -> None:
    """Launch an out-of-band monitor agent subprocess and report its result.

    The shared subprocess machinery for the check, gate, and curator flavors:
    Popen (never blocking the scheduler thread) plus a waiter thread that
    tees stdout to manager.log and hands ``parse(stdout)`` - or None on
    spawn/timeout failure, indeterminate - to ``on_result``. ``cleanup`` (if
    given) runs exactly once when the subprocess is finished with its inputs.
    """
    from bobi import paths
    root = paths.bobi_root()
    log_path = paths.state_dir() / "manager.log"

    oversized = [(idx, len(str(arg).encode())) for idx, arg in enumerate(cmd)
                 if len(str(arg).encode()) > MAX_MONITOR_ARG_BYTES]
    if oversized:
        idx, size = oversized[0]
        detail = (f"argv element {idx} is {size} bytes, over "
                  f"{MAX_MONITOR_ARG_BYTES} byte safety bound")
        message = (f"Failed to spawn {kind} for monitor {monitor_name}: "
                   f"{detail}")
        log.error(message)
        _append_manager_log(message)
        if cleanup:
            cleanup()
        _publish_monitor_error(
            monitor_name, kind, "spawn-failed", detail, publish=publish)
        on_result(None)
        return

    try:
        with open(log_path, "a") as lf:
            # cwd pins the child CLI's root resolution to this process's
            # bound root - never whatever directory the manager happened
            # to be started from.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=lf,
                                    text=True, start_new_session=True,
                                    cwd=str(root))
    except OSError as e:
        detail = str(e)
        message = f"Failed to spawn {kind} for monitor {monitor_name}: {detail}"
        log.error(message)
        _append_manager_log(message)
        if cleanup:
            cleanup()
        _publish_monitor_error(
            monitor_name, kind, "spawn-failed", detail, publish=publish)
        on_result(None)
        return

    def _wait() -> None:
        from bobi.subagent import CHECK_TIMEOUT
        # The blocking runners retry internally (attempts=2, each bounded by
        # CHECK_TIMEOUT), so allow both attempts plus startup slack.
        budget = 2 * CHECK_TIMEOUT + 120
        try:
            out, _ = proc.communicate(timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            detail = f"exceeded {budget}s - killed"
            message = f"{kind.capitalize()} for monitor {monitor_name} {detail}"
            log.error(message)
            _append_manager_log(message)
            _publish_monitor_error(
                monitor_name, kind, "timeout", detail, publish=publish)
            on_result(None)
            return
        finally:
            if cleanup:
                cleanup()
        if out:  # keep the agent's output observable in manager.log
            try:
                with open(log_path, "a") as lf:
                    lf.write(out)
            except OSError:
                pass
        result = parse(out)
        if result is None:
            detail = "subprocess output did not contain a parseable result"
            log.warning("Monitor %s: %s %s", monitor_name, kind, detail)
            _append_manager_log(
                f"Monitor {monitor_name}: {kind} {detail}")
            _publish_monitor_error(
                monitor_name, kind, "indeterminate-result", detail,
                publish=publish)
        on_result(result)

    threading.Thread(target=_wait, daemon=True,
                     name=f"{kind}-wait-{monitor_name}").start()


def _default_spawn_check(monitor, cwd: str | None, on_verdict,
                         publish=None) -> None:
    """Launch a non-interactive check subprocess and report its verdict.

    Runs `bobi agent <name> subagents launch --wait` out-of-band so the scheduler thread
    is never blocked, captures the check's stdout, and hands the trailing
    verdict JSON (or None) to ``on_verdict`` from a waiter thread when the
    process exits. The check agent only observes — converting the verdict to
    conditions, dedup, and publishing all happen in the scheduler.
    """
    role = _resolve_monitor_role(monitor)
    from bobi import paths
    root = paths.bobi_root()
    cmd = [
        sys.executable, "-m", "bobi.cli",
        "agent", paths.agent_name_for_root(root), "subagents", "launch",
        "-w", "adhoc",
        *(["--role", role] if role else []),
        "--non-interactive",
        "--wait",
        "--task", monitor.description or monitor.name,
    ]
    _spawn_monitor_agent(
        cmd, monitor.name, "check", _parse_verdict, on_verdict,
        publish=publish)


# A gate request file older than this is an orphan: the waiter thread that
# would have deleted it died with a previous manager. Twice the waiter budget
# leaves generous slack for a live in-flight gate.
_GATE_REQUEST_MAX_AGE = 3600 * 2
_CURATOR_TASK_MAX_AGE = 3600 * 2


def _write_gate_request(monitor, items: list) -> str | None:
    """Write the gate request file (criterion + new items) and return its path.

    Item payloads are cut to the gate prompt's per-item budget at write time -
    the prompt would truncate them anyway, so a batch of large emails must not
    turn into megabytes written and re-parsed per gate call. The full payload
    still publishes: the scheduler keeps the original conditions in memory for
    the verdict callback. Also sweeps orphaned request files left by a manager
    that died mid-gate, so raw payloads never accumulate at rest.
    """
    import tempfile
    import time as time_mod

    from bobi import paths
    from bobi.subagent import GATE_ITEM_CHARS

    gates_dir = paths.state_dir() / "gates"
    try:
        gates_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time_mod.time() - _GATE_REQUEST_MAX_AGE
        for stale in gates_dir.glob("*.json"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass

        rendered = []
        for c in items:
            try:
                serialized = json.dumps(c.data, sort_keys=True, default=str)
            except (TypeError, ValueError):
                serialized = str(c.data)
            data = (c.data if len(serialized) <= GATE_ITEM_CHARS
                    else {"truncated_payload": serialized[:GATE_ITEM_CHARS]})
            rendered.append({"key": c.key, "data": data})

        fd, request_path = tempfile.mkstemp(
            dir=gates_dir, prefix=f"{monitor.name.replace('/', '_')}-",
            suffix=".json")
        try:
            with open(fd, "w") as f:
                json.dump({
                    "criterion": monitor.relevance,
                    "name": f"gate-{monitor.name}",
                    "items": rendered,
                }, f, default=str)
        except (OSError, TypeError, ValueError):
            Path(request_path).unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError) as e:
        log.error(f"Failed to write gate request for monitor {monitor.name}: {e}")
        return None
    return request_path


def _write_curator_task(monitor, task: str) -> str | None:
    """Write a rendered curator task to disk and return its absolute path.

    Curator prompts include transcript windows up to hundreds of KB, which can
    exceed Linux's per-argv-string limit if passed inline. Store the full task
    under state/curator and pass the agent a short read-this-file instruction.
    """
    from bobi import paths

    tasks_dir = paths.state_dir() / "curator"
    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time_mod.time() - _CURATOR_TASK_MAX_AGE
        for stale in tasks_dir.glob("task-*.md"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                pass

        fd, task_path = tempfile.mkstemp(
            dir=tasks_dir, prefix="task-", suffix=".md")
        try:
            with open(fd, "w") as f:
                f.write(task)
        except OSError:
            Path(task_path).unlink(missing_ok=True)
            raise
    except OSError as e:
        log.error("Failed to write curator task for monitor %s: %s",
                  monitor.name, e)
        return None
    return task_path


def _default_spawn_gate(monitor, cwd: str | None, items: list, on_verdict,
                        publish=None) -> None:
    """Launch a non-interactive relevance-gate subprocess over new items (#630).

    Mirrors _default_spawn_check: out-of-band so the scheduler thread is never
    blocked, cost attributed to role=monitor. The batch of new items rides in
    a request file under run/state/gates/ because real payloads (emails) do
    not fit argv. The waiter thread hands the trailing verdict JSON (or None)
    to ``on_verdict`` when the process exits; judging which keys publish stays
    in the scheduler.
    """
    from bobi import paths

    request_path = _write_gate_request(monitor, items)
    if request_path is None:
        on_verdict(None)
        return

    root = paths.bobi_root()
    cmd = [
        sys.executable, "-m", "bobi.cli",
        "agent", paths.agent_name_for_root(root), "monitors", "gate",
        "--request", str(request_path),
    ]
    _spawn_monitor_agent(
        cmd, monitor.name, "gate", _parse_gate_output, on_verdict,
        cleanup=lambda: Path(request_path).unlink(missing_ok=True),
        publish=publish,
    )


def _default_spawn_curator(monitor, cwd: str | None, task: str, on_result,
                           publish=None) -> None:
    """Launch the out-of-band curator agent with a pre-rendered task (#456).

    Mirrors _default_spawn_check, but the agent WRITES policy.md (its Write tool
    is available because adhoc launches run permission_mode=bypassPermissions)
    and prints a JSON summary instead of a verdict. The waiter thread hands the
    parsed summary (or None) to ``on_result`` when the process exits. The
    scheduler — not the agent — owns the cursor advance and the publish.
    """
    role = _resolve_monitor_role(monitor)
    from bobi import paths

    task_path = _write_curator_task(monitor, task)
    if task_path is None:
        _publish_monitor_error(
            monitor.name, "curator", "spawn-failed",
            "failed to write curator task file", publish=publish)
        on_result(None)
        return

    root = paths.bobi_root()
    pointer_task = (
        "Read the monitor task file at this absolute path and follow its "
        f"instructions exactly:\n\n{task_path}\n\n"
        "Do not treat this pointer as the full task; the full curator prompt, "
        "current policy, transcript delta, and ingest notes are in the file."
    )
    cmd = [
        sys.executable, "-m", "bobi.cli",
        "agent", paths.agent_name_for_root(root), "subagents", "launch",
        "-w", "adhoc",
        *(["--role", role] if role else []),
        "--non-interactive",
        "--wait",
        "--agent-wait",
        "--task", pointer_task,
    ]

    def _parse_curator(out: str):
        from bobi.monitors import curator as curator_mod
        return curator_mod.parse_result(out)

    _spawn_monitor_agent(
        cmd, monitor.name, "curator", _parse_curator, on_result,
        cleanup=lambda: Path(task_path).unlink(missing_ok=True),
        publish=publish,
    )


class MonitorScheduler:
    def __init__(self, publish=None, state_path: Path | None = None,
                 now=None, registry_loader=None, spawn_check=None,
                 project_path: Path | None = None, spawn_curator=None,
                 spawn_gate=None):
        self.publish = publish or _default_publish
        self.spawn_check = spawn_check or (
            lambda monitor, cwd, on_verdict: _default_spawn_check(
                monitor, cwd, on_verdict, publish=self.publish)
        )
        self.spawn_curator = spawn_curator or (
            lambda monitor, cwd, task, on_result: _default_spawn_curator(
                monitor, cwd, task, on_result, publish=self.publish)
        )
        self.spawn_gate = spawn_gate or (
            lambda monitor, cwd, items, on_verdict: _default_spawn_gate(
                monitor, cwd, items, on_verdict, publish=self.publish)
        )
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
        # Monitors with a relevance gate verdict still pending. In-memory on
        # purpose: a manager restart mid-gate recorded nothing, so re-gating
        # the same items is safe and loses nothing.
        self._gates_in_flight: set[str] = set()
        # Consecutive indeterminate gate verdicts per monitor, to surface a
        # systematically broken gate (each retry pays for a model call that
        # never lands a verdict). Reset on any successful verdict.
        self._gate_failures: dict[str, int] = {}
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

        Plain daily `at:` monitors catch up: a slot missed while the manager
        was down fires once, late, on the next tick. A weekday-gated (`days:`)
        weekly monitor does NOT catch up (D8) — if the manager was down across
        the scheduled instant, that run is skipped and only the next scheduled
        occurrence fires. The two are told apart by how late `now` is relative
        to the scheduled instant: a live fire lands within ~a tick of it, a
        catch-up after downtime lands much later.
        """
        try:
            scheduled = self._last_scheduled(monitor, now)
        except ValueError as e:
            log.warning(f"Monitor {monitor.name} has a bad at/days schedule: {e}")
            return False
        last = _parse_iso(last_run) if last_run else None
        if last is None:
            self._rebaseline_at(monitor, now)
            return False
        if scheduled <= last:
            return False
        if monitor.weekdays and (now - scheduled).total_seconds() > _AT_CATCHUP_GRACE:
            # Weekly schedule, instant passed while we were down — skip it,
            # rebaseline past the missed slot, fire at the next occurrence.
            self._rebaseline_at(monitor, now)
            return False
        return True

    def _rebaseline_at(self, monitor, now: datetime) -> None:
        """Record an at-monitor's baseline without firing — so a passed slot
        isn't retro-fired on first sight or after a skipped weekly run."""
        with self._state_lock:
            self.state.setdefault(monitor.state_key, {})["last_run"] = now.isoformat()
            self._save_state()

    @staticmethod
    def _last_scheduled(monitor, now: datetime) -> datetime:
        """The most recent scheduled fire time at or before `now`, computed in
        the monitor's timezone (so '06:00' means 6am in `tz`, not UTC).

        When `days:` is set, only those weekdays are eligible: for each `at:`
        time we walk back day-by-day (rebuilding the wall-clock instant on each
        local date, so DST shifts stay correct) to the most recent allowed
        weekday at/before `now`. Empty `days:` ⇒ every weekday eligible, which
        reduces to the original "most recent at-time within the last day"."""
        from datetime import timedelta

        local = now.astimezone(monitor.tzinfo)
        weekdays = monitor.weekdays  # empty set ⇒ no gating (every day)
        base_date = local.date()
        candidates = []
        for hour, minute in monitor.at_times:
            # Search back up to a full week for the most recent eligible
            # (weekday, at-time) instant at or before `now`. Range 0..7 covers
            # every weekday, so a gated monitor always finds a candidate.
            for delta in range(8):
                day = base_date - timedelta(days=delta)
                t = local.replace(year=day.year, month=day.month, day=day.day,
                                  hour=hour, minute=minute,
                                  second=0, microsecond=0)
                if t > local:
                    continue  # at-time hasn't arrived yet on this date
                if weekdays and t.weekday() not in weekdays:
                    continue  # not an eligible weekday
                candidates.append(t)
                break
        return max(candidates)

    def run_monitor(self, monitor, registry: MonitorRegistry, now: datetime) -> None:
        """Detect conditions for one monitor and reconcile them.

        Detection is the only flavor-specific step. Description-only checks
        detect out-of-band: nothing reconciles here, the waiter thread calls
        back into _reconcile when the verdict lands.
        """
        curator_spawned = False
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
        elif monitor.curator:
            # The curator writes an artifact, not a verdict — it does not flow
            # through _reconcile. The cursor advance + publish happen on result.
            curator_spawned = self._spawn_curator(
                monitor, registry.projects_for(monitor))
            conditions = None
        else:
            self._spawn_check(monitor, registry.projects_for(monitor))
            conditions = None  # detection in flight — reconciled on verdict

        if conditions is not None:
            if monitor.gated:
                # Two-tier semantic gate (#630): the mechanical detector
                # decided what exists, a cheap-model gate judges what is new.
                self._reconcile_gated(monitor, conditions,
                                      registry.projects_for(monitor))
            else:
                self._reconcile(monitor, conditions)

        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            entry["last_run"] = now.isoformat()
            if monitor.curator and curator_spawned:
                entry["last_spawn"] = now.isoformat()
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
        ok = self.publish(event, {
            **condition.data,
            "monitor": monitor.name,
            "finding_key": str(condition.key),
        })
        if ok:
            log.info(f"Monitor {monitor.name} fired {event} ({condition.key})")
        else:
            log.warning(f"Monitor {monitor.name} failed to publish {event} "
                        f"({condition.key}) — will retry next interval")
        return ok

    # --- relevance gate (two-tier semantic gate, #630) -------------------

    def _reconcile_gated(self, monitor, conditions: list,
                         projects: list[Path]) -> None:
        """Dedup first, then judge ONLY the new conditions with a cheap-model
        relevance gate before anything publishes.

        The cost shape is the point: a tick where the mechanical detector
        finds nothing new ends here with zero LLM calls (the common case).
        Only genuinely new keys ride to an out-of-band gate agent; the
        verdict callback publishes the relevant ones and records the
        irrelevant ones active WITHOUT publishing, so each item is judged
        exactly once. A judged-relevant item whose publish failed sits in
        ``pending_publish`` and is retried here mechanically at $0 - never
        re-sent to the model, whose second opinion on a borderline item
        could flip and silently drop the finding. Clearing semantics match
        _reconcile: disappeared keys drop out and re-fire if they recur.
        """
        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            previous = set(entry.get("active", []))
            current = {c.key: c for c in conditions}
            entry["active"] = [k for k in current if k in previous]
            self._save_state()
            pending = dict(entry.get("pending_publish") or {})
            new = [current[k] for k in current
                   if k not in previous and k not in pending]
            spawn = False
            if new:
                if monitor.state_key in self._gates_in_flight:
                    # A verdict is still pending. The unrecorded keys stay
                    # new and re-enter here on the tick after it lands.
                    log.info(f"Monitor {monitor.name}: gate already in "
                             f"flight - deferring {len(new)} new item(s)")
                else:
                    if len(new) > GATE_MAX_ITEMS:
                        log.info(f"Monitor {monitor.name}: {len(new)} new "
                                 f"items - gating the first {GATE_MAX_ITEMS},"
                                 " the rest stay new for the next interval")
                        new = new[:GATE_MAX_ITEMS]
                    self._gates_in_flight.add(monitor.state_key)
                    spawn = True

        if pending:
            # Already judged relevant, publish failed last time: retry the
            # publish only, outside the lock.
            self._publish_judged(monitor, [Condition(key=k, data=d)
                                           for k, d in pending.items()])

        if not spawn:
            return
        cwd = str(projects[0]) if projects else None
        log.info(f"Monitor {monitor.name}: gating {len(new)} new item(s) "
                 "against its relevance criterion")
        try:
            self.spawn_gate(monitor, cwd, new,
                            lambda verdict: self._on_gate_verdict(monitor, new, verdict))
        except Exception as e:
            # A spawn that raised will never deliver a verdict - lift the
            # in-flight guard so the next tick can retry.
            with self._state_lock:
                self._gates_in_flight.discard(monitor.state_key)
            log.error(f"Failed to spawn gate for monitor {monitor.name}: {e}")

    def _publish_judged(self, monitor, conditions: list) -> None:
        """Publish judged-relevant conditions and record the outcome.

        Publishes run OUTSIDE the state lock - they are HTTP posts (up to
        GATE_MAX_ITEMS of them) and must never stall the scheduler tick or
        the check-waiter threads queued on the lock. A successful publish
        records the key active and clears any pending_publish entry; a
        failed one parks the payload in pending_publish for a mechanical
        retry next tick.
        """
        fired = {c.key for c in conditions if self._fire(monitor, c)}
        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            active = list(entry.get("active", []))
            pending = dict(entry.get("pending_publish") or {})
            for c in conditions:
                if c.key in fired:
                    pending.pop(c.key, None)
                    if c.key not in active:
                        active.append(c.key)
                else:
                    pending[c.key] = c.data
            entry["active"] = active
            if pending:
                entry["pending_publish"] = pending
            else:
                entry.pop("pending_publish", None)
            self._save_state()

    def _on_gate_verdict(self, monitor, judged: list,
                         verdict: dict | None) -> None:
        """Reconcile a relevance-gate verdict (waiter-thread callback).

        Irrelevant items are recorded active without publishing - judged
        once, never re-judged. Relevant items publish via _publish_judged;
        a failed publish parks the payload for a mechanical retry, never a
        re-judge. An indeterminate gate records nothing, so every judged
        key stays new and the next tick retries the judgment. The in-flight
        guard is held until the publishes are recorded, so a concurrent
        tick can neither re-gate nor double-publish the judged keys.
        """
        key = monitor.state_key
        if not isinstance(verdict, dict) or not verdict.get("success", False):
            with self._state_lock:
                self._gates_in_flight.discard(key)
            failures = self._gate_failures.get(key, 0) + 1
            self._gate_failures[key] = failures
            log.warning(f"Monitor {monitor.name}: gate indeterminate - "
                        "leaving new items unjudged, retrying next interval")
            if failures >= 3:
                log.error(f"Monitor {monitor.name}: {failures} consecutive "
                          "indeterminate gates - every interval pays for a "
                          "verdict that never lands and nothing publishes; "
                          "check the monitor role's model and the gate "
                          "output in manager.log")
            return
        self._gate_failures.pop(key, None)

        raw = verdict.get("relevant", [])
        relevant = {str(k) for k in raw} if isinstance(raw, list) else set()
        to_publish = [c for c in judged if c.key in relevant]

        with self._state_lock:
            entry = self.state.setdefault(key, {})
            active = list(entry.get("active", []))
            for condition in judged:
                if condition.key not in relevant and condition.key not in active:
                    active.append(condition.key)
            entry["active"] = active
            self._save_state()

        if to_publish:
            self._publish_judged(monitor, to_publish)

        with self._state_lock:
            self._gates_in_flight.discard(key)

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

    # --- curator flavor (#456) -----------------------------------------

    def _project_root(self, projects: list[Path]):
        """The bound project root for path resolution — the first applicable
        project, or the scheduler's own bound root."""
        if projects:
            return projects[0]
        return self._project_path

    def _load_curator_prompt(self, root) -> str:
        """The curator agent's working instructions. Team override first
        (<run>/package/prompts/curator.md), framework default otherwise —
        "what counts as durable" is domain-flavored (Q1), so a team can replace
        it without touching the framework."""
        from bobi import paths
        from bobi.prompts import CURATOR_PATH
        if root:
            override = paths.package_dir(root) / "prompts" / "curator.md"
            try:
                if override.is_file():
                    return override.read_text()
            except OSError:
                pass
        try:
            return CURATOR_PATH.read_text()
        except OSError:
            log.error("Curator prompt missing at %s", CURATOR_PATH)
            return ""

    def _spawn_curator(self, monitor, projects: list[Path]) -> bool:
        """Window the transcript delta, apply the input cap, and launch the
        curator agent with the rendered delta (#456).

        The scheduler owns the deterministic half — read the success-advanced
        cursor, index new transcript lines, select messages oldest-first by id
        under MAX_CURATOR_INPUT_CHARS (deferring the overflow, truncating an
        oversized oldest message) — so the cursor / cap / no-silent-skip
        invariants live in plain code, never behind the model. The agent does
        the judgment and rewrites policy.md; _on_curator_result advances the
        cursor and publishes on success.
        """
        from bobi import history, paths
        from bobi.memory import collect_legacy_journals, load_policy
        from bobi.monitors import curator as curator_mod

        root = self._project_root(projects)
        state_dir = paths.state_path(root)
        cursor_path = paths.policy_cursor_path(root)
        cursor = curator_mod.read_cursor(cursor_path)

        try:
            history.index()  # incremental — only new JSONL lines
        except Exception as e:
            log.warning("Curator transcript index failed for %s: %s", monitor.name, e)

        rows = history.messages_since(cursor)

        # One-time seed (#456): on the very first run (no policy.md yet) distill
        # the existing per-session decision-log journals into the first policy.md
        # so accumulated knowledge isn't discarded at rollout. Guarded on
        # policy.md absence → idempotent: once written, the seed never re-fires.
        seed = ""
        if not paths.policy_path(root).is_file():
            seed = collect_legacy_journals(state_dir, curator_mod.MAX_SEED_INPUT_CHARS)

        if not rows and not seed:
            log.info("Monitor %s due - no new transcript messages since cursor %d "
                     "and nothing to seed", monitor.name, cursor)
            return False

        ingested, highest_id, flags = curator_mod.select_messages(
            rows, curator_mod.MAX_CURATOR_INPUT_CHARS)
        if highest_id is None and not seed:
            log.info("Monitor %s: nothing ingestable this run", monitor.name)
            return False

        transcript = curator_mod.render_transcript(ingested)
        try:
            current_policy = load_policy(state_dir)
        except Exception:
            current_policy = ""
        task = curator_mod.build_curator_task(
            self._load_curator_prompt(root), transcript, current_policy, flags, seed=seed)
        if seed:
            log.info("Monitor %s: seeding first policy.md from %d chars of legacy "
                     "journals", monitor.name, len(seed))

        cwd = str(projects[0]) if projects else None
        log.info("Monitor %s due — spawning curator over %d new message(s) "
                 "(highest id %d, deferred=%s)",
                 monitor.name, len(ingested), highest_id, flags.get("input_truncated"))
        self.spawn_curator(
            monitor, cwd, task,
            lambda result: self._on_curator_result(
                monitor, result, highest_id, cursor_path),
        )
        return True

    def _on_curator_result(self, monitor, result: dict | None,
                           highest_id: int | None, cursor_path: Path) -> None:
        """Waiter-thread callback for a finished curator run.

        Advances the cursor ONLY on success (a failed/indeterminate run leaves
        it unmoved so the same window is re-read next interval — no transcript
        skipped). Publishes `system/policy.updated` only when the run actually
        changed policy.md.
        """
        from bobi.monitors import curator as curator_mod

        if not isinstance(result, dict):
            log.warning("Monitor %s: curator run failed/indeterminate — cursor "
                        "NOT advanced, retrying next interval", monitor.name)
            return
        if not result.get("success"):
            summary = str(result.get("summary", "") or "curator returned failure")
            log.warning("Monitor %s: curator run failed — cursor NOT advanced, "
                        "retrying next interval: %s", monitor.name, summary)
            _publish_monitor_error(
                monitor.name, "curator", "indeterminate-result", summary,
                publish=self.publish)
            return

        # A seed-only first run ingests no transcript rows (highest_id is None) —
        # there is nothing to advance; the cursor stays at 0 and the next run
        # reads the real transcript delta normally.
        if highest_id is not None:
            try:
                curator_mod.write_cursor(cursor_path, highest_id)
            except OSError as e:
                log.error("Monitor %s: failed to advance curator cursor: %s",
                          monitor.name, e)

        if result.get("lossy_drops"):
            log.warning("Monitor %s: curator made %s LOSSY drop(s) of still-valid "
                        "items for space — raise MAX_POLICY_CHARS / build the "
                        "decisions-spill: %s", monitor.name,
                        result.get("lossy_drops"), result.get("summary", ""))

        if result.get("updated"):
            self._publish_policy_updated(monitor, result)
        else:
            log.info("Monitor %s: curator found nothing durable — no publish",
                     monitor.name)

    def _publish_policy_updated(self, monitor, result: dict) -> None:
        """Publish the completion event directly (bypassing _reconcile dedup).

        A completion signal is not a deduped finding — two runs with the same
        summary must both deliver. The drain-side filter (events/drain.py)
        enforces passive-vs-active: a non-urgent policy.updated publishes for
        observability but is suppressed before the inbox push; urgent ones push.
        """
        event = monitor.event or "system/policy.updated"
        payload = {
            "monitor": monitor.name,
            "summary": str(result.get("summary", "")),
            "bytes": int(result.get("bytes", 0) or 0),
            "urgent": bool(result.get("urgent", False)),
        }
        if self.publish(event, payload):
            log.info("Monitor %s published %s (urgent=%s)",
                     monitor.name, event, payload["urgent"])
        else:
            log.warning("Monitor %s failed to publish %s", monitor.name, event)

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
