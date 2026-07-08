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
  - Sleep cycle (`sleep_cycle: true`) — the one flavor whose agent WRITES an artifact
    (long_term_memory.md) instead of returning a verdict (#456). The scheduler does the
    deterministic half (window the transcript delta on messages.id since a
    success-advanced cursor, apply the per-run input cap), launches the sleep_cycle
    agent with the rendered delta, and on a successful run advances the cursor
    and publishes `system/memory.updated` directly — bypassing _reconcile dedup
    because a completion signal is not a deduped finding.
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


def _resolve_monitor_role(monitor) -> str:
    """Resolve the role for a monitor-launched subagent.

    Monitor-local ``role`` wins. If omitted, use the installed agent's
    ``entry_point`` so framework defaults can run without duplicating a role in
    every monitor record.
    """
    role = getattr(monitor, "role", "") or ""
    if role:
        return role
    try:
        from bobi.config import Config
        from bobi.paths import bound_root as get_project_root
        root = get_project_root()
        if root:
            cfg = Config.load(root)
            return cfg.entry_point or ""
    except Exception:
        pass
    return ""


def _parse_verdict(output: str) -> dict | None:
    """Extract the trailing verdict JSON a check process printed, or None.

    `bobi agent <name> subagents launch --wait` prints the check's verdict as a single
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

    log_path = paths.state_dir() / "manager.log"

    try:
        with open(log_path, "a") as lf:
            # cwd pins the child CLI's root resolution to this process's
            # bound root — never whatever directory the manager happened
            # to be started from.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=lf,
                                    text=True, start_new_session=True,
                                    cwd=str(root))
    except OSError as e:
        log.error(f"Failed to spawn check for monitor {monitor.name}: {e}")
        return

    def _wait() -> None:
        from bobi.subagent import CHECK_TIMEOUT
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


def _default_spawn_sleep_cycle(monitor, cwd: str | None, task: str, on_result) -> None:
    """Launch the out-of-band sleep cycle agent with a pre-rendered task (#456).

    Mirrors _default_spawn_check, but the agent WRITES long_term_memory.md (its Write tool
    is available because adhoc launches run permission_mode=bypassPermissions)
    and prints a JSON summary instead of a verdict. The waiter thread hands the
    parsed summary (or None) to ``on_result`` when the process exits. The
    scheduler — not the agent — owns the cursor advance and the publish.
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
        "--agent-wait",
        "--task", task,
    ]

    log_path = paths.state_dir() / "manager.log"

    try:
        with open(log_path, "a") as lf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=lf,
                                    text=True, start_new_session=True,
                                    cwd=str(root))
    except OSError as e:
        log.error(f"Failed to spawn sleep_cycle for monitor {monitor.name}: {e}")
        on_result(None)
        return

    def _wait() -> None:
        from bobi.subagent import CHECK_TIMEOUT
        budget = 2 * CHECK_TIMEOUT + 120
        try:
            out, _ = proc.communicate(timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            log.error(f"Sleep cycle for monitor {monitor.name} exceeded {budget}s — killed")
            on_result(None)
            return
        if out:
            try:
                with open(log_path, "a") as lf:
                    lf.write(out)
            except OSError:
                pass
        from bobi.monitors import sleep_cycle as sleep_cycle_mod
        on_result(sleep_cycle_mod.parse_result(out))

    threading.Thread(target=_wait, daemon=True,
                     name=f"sleep_cycle-wait-{monitor.name}").start()


class MonitorScheduler:
    def __init__(self, publish=None, state_path: Path | None = None,
                 now=None, registry_loader=None, spawn_check=None,
                 project_path: Path | None = None, spawn_sleep_cycle=None,
                 spawn_curator=None):
        self.publish = publish or _default_publish
        self.spawn_check = spawn_check or _default_spawn_check
        self.spawn_sleep_cycle = (
            spawn_sleep_cycle or spawn_curator or _default_spawn_sleep_cycle
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
        sleep_cycle_spawned = False
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
        elif monitor.sleep_cycle:
            # The sleep_cycle writes an artifact, not a verdict — it does not flow
            # through _reconcile. The cursor advance + publish happen on result.
            sleep_cycle_spawned = self._spawn_sleep_cycle(
                monitor, registry.projects_for(monitor))
            conditions = None
        else:
            self._spawn_check(monitor, registry.projects_for(monitor))
            conditions = None  # detection in flight — reconciled on verdict

        if conditions is not None:
            self._reconcile(monitor, conditions)

        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            entry["last_run"] = now.isoformat()
            if monitor.sleep_cycle and sleep_cycle_spawned:
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

    # --- sleep_cycle flavor (#456) -----------------------------------------

    def _project_root(self, projects: list[Path]):
        """The bound project root for path resolution — the first applicable
        project, or the scheduler's own bound root."""
        if projects:
            return projects[0]
        return self._project_path

    def _load_sleep_cycle_prompt(self, root) -> str:
        """The sleep cycle agent's working instructions. Team override first
        (<run>/package/prompts/sleep_cycle.md), then the legacy
        <run>/package/prompts/curator.md override, framework default otherwise —
        "what counts as durable" is domain-flavored (Q1), so a team can replace
        it without touching the framework."""
        from bobi import paths
        from bobi.prompts import SLEEP_CYCLE_PATH
        if root:
            prompts_dir = paths.package_dir(root) / "prompts"
            for name in ("sleep_cycle.md", "curator.md"):
                override = prompts_dir / name
                try:
                    if override.is_file():
                        return override.read_text()
                except OSError:
                    pass
        try:
            return SLEEP_CYCLE_PATH.read_text()
        except OSError:
            log.error("Sleep cycle prompt missing at %s", SLEEP_CYCLE_PATH)
            return ""

    def _spawn_sleep_cycle(self, monitor, projects: list[Path]) -> bool:
        """Window the transcript delta, apply the input cap, and launch the
        sleep cycle agent with the rendered delta (#456).

        The scheduler owns the deterministic half — read the success-advanced
        cursor, index new transcript lines, select messages oldest-first by id
        under MAX_SLEEP_CYCLE_INPUT_CHARS (deferring the overflow, truncating an
        oversized oldest message) — so the cursor / cap / no-silent-skip
        invariants live in plain code, never behind the model. The agent does
        the judgment and rewrites long_term_memory.md; _on_sleep_cycle_result advances the
        cursor and publishes on success.
        """
        from bobi import history, paths
        from bobi.memory import collect_legacy_journals, load_long_term_memory
        from bobi.monitors import sleep_cycle as sleep_cycle_mod

        root = self._project_root(projects)
        paths.migrate_long_term_memory_state(root)
        state_dir = paths.state_path(root)
        cursor_path = paths.long_term_memory_cursor_path(root)
        cursor = sleep_cycle_mod.read_cursor(cursor_path)

        try:
            history.index()  # incremental — only new JSONL lines
        except Exception as e:
            log.warning("Sleep cycle transcript index failed for %s: %s", monitor.name, e)

        rows = history.messages_since(cursor)

        # One-time seed (#456): on the very first run (no long_term_memory.md yet) distill
        # the existing per-session decision-log journals into the first long_term_memory.md
        # so accumulated knowledge isn't discarded at rollout. Guarded on
        # long_term_memory.md absence → idempotent: once written, the seed never re-fires.
        seed = ""
        if not paths.long_term_memory_path(root).is_file():
            seed = collect_legacy_journals(state_dir, sleep_cycle_mod.MAX_SEED_INPUT_CHARS)

        if not rows and not seed:
            log.info("Monitor %s due — no new transcript messages since cursor %d "
                     "and nothing to seed", monitor.name, cursor)
            return False

        ingested, highest_id, flags = sleep_cycle_mod.select_messages(
            rows, sleep_cycle_mod.MAX_SLEEP_CYCLE_INPUT_CHARS)
        if highest_id is None and not seed:
            log.info("Monitor %s: nothing ingestable this run", monitor.name)
            return False

        transcript = sleep_cycle_mod.render_transcript(ingested)
        try:
            current_memory = load_long_term_memory(state_dir)
        except Exception:
            current_memory = ""
        task = sleep_cycle_mod.build_sleep_cycle_task(
            self._load_sleep_cycle_prompt(root), transcript, current_memory, flags, seed=seed)
        if seed:
            log.info("Monitor %s: seeding first long_term_memory.md from %d chars of legacy "
                     "journals", monitor.name, len(seed))

        cwd = str(projects[0]) if projects else None
        log.info("Monitor %s due — spawning sleep_cycle over %d new message(s) "
                 "(highest id %d, deferred=%s)",
                 monitor.name, len(ingested), highest_id, flags.get("input_truncated"))
        self.spawn_sleep_cycle(
            monitor, cwd, task,
            lambda result: self._on_sleep_cycle_result(
                monitor, result, highest_id, cursor_path),
        )
        return True

    def _on_sleep_cycle_result(self, monitor, result: dict | None,
                           highest_id: int | None, cursor_path: Path) -> None:
        """Waiter-thread callback for a finished sleep cycle run.

        Advances the cursor ONLY on success (a failed/indeterminate run leaves
        it unmoved so the same window is re-read next interval — no transcript
        skipped). Publishes `system/memory.updated` only when the run actually
        changed long_term_memory.md.
        """
        from bobi.monitors import sleep_cycle as sleep_cycle_mod

        if not isinstance(result, dict) or not result.get("success"):
            log.warning("Monitor %s: sleep cycle run failed/indeterminate — cursor "
                        "NOT advanced, retrying next interval", monitor.name)
            return

        # A seed-only first run ingests no transcript rows (highest_id is None) —
        # there is nothing to advance; the cursor stays at 0 and the next run
        # reads the real transcript delta normally.
        if highest_id is not None:
            try:
                sleep_cycle_mod.write_cursor(cursor_path, highest_id)
            except OSError as e:
                log.error("Monitor %s: failed to advance sleep_cycle cursor: %s",
                          monitor.name, e)

        if result.get("lossy_drops"):
            log.warning("Monitor %s: sleep_cycle made %s LOSSY drop(s) of still-valid "
                        "items for space — raise MAX_MEMORY_CHARS / build the "
                        "decisions-spill: %s", monitor.name,
                        result.get("lossy_drops"), result.get("summary", ""))

        if result.get("updated"):
            self._publish_memory_updated(monitor, result)
        else:
            log.info("Monitor %s: sleep_cycle found nothing durable — no publish",
                     monitor.name)

    def _publish_memory_updated(self, monitor, result: dict) -> None:
        """Publish the completion event directly (bypassing _reconcile dedup).

        A completion signal is not a deduped finding — two runs with the same
        summary must both deliver. The drain-side filter (events/drain.py)
        enforces passive-vs-active: a non-urgent memory.updated publishes for
        observability but is suppressed before the inbox push; urgent ones push.
        """
        event = monitor.event or "system/memory.updated"
        payload = {
            "monitor": monitor.name,
            "summary": str(result.get("summary", "")),
            "bytes": int(result.get("bytes", 0) or 0),
            "urgent": bool(result.get("urgent", False)),
        }
        published = self.publish(event, payload)
        if event != "system/policy.updated":
            self.publish("system/policy.updated", payload)
        if published:
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
