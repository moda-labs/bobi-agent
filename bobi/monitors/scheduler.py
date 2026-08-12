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

A condition is recorded active only after its event actually publishes. A
failed publish (event server briefly down) does not vanish: its payload is
parked in `pending_publish` and the tick drain retries the publish alone -
no detector, no model, no schedule - until it lands or the detector stops
reporting the condition. Draining from the tick rather than from a firing is
what makes that true for a scheduled monitor, whose "next interval" is a day
away (#1006).

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
  - Sleep cycle (`sleep_cycle: true`) — the one flavor whose agent WRITES an
    artifact (long_term_memory.md) instead of returning a verdict (#456). The
    scheduler does the deterministic half (window the transcript delta on
    messages.id since a success-advanced cursor, apply the per-run input cap),
    launches the sleep-cycle agent with the rendered delta, and on a successful
    run advances the cursor and publishes `system/memory.updated` directly —
    bypassing _reconcile dedup
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bobi.fsutil import atomic_write_json


def _load_framework_checks() -> dict:
    """Load check runners bundled with the framework (bobi/monitors/*_checks.py).

    These files are ordinary submodules of this package and are globbed under
    their canonical names, so ``import_module`` already does the
    sys.modules-cache-then-load dance. The spec/loader machinery in
    :func:`_load_checks` is for the pack-level ``bobi_checks.*`` files, which
    have no importable package parent — it is needed there, not here.
    """
    checks: dict = {}
    framework_dir = Path(__file__).parent
    for py_file in framework_dir.glob("*_checks.py"):
        mod = importlib.import_module(f"bobi.monitors.{py_file.stem}")
        if hasattr(mod, "CHECKS"):
            checks.update(mod.CHECKS)
    return checks


def _facts_section_has_reference_pointer(content: str) -> bool:
    """Return true when the reference pointer appears as a Facts bullet."""
    in_facts = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Facts":
            in_facts = True
            continue
        if stripped.startswith("## ") and in_facts:
            return False
        if in_facts and stripped.startswith(("-", "*")):
            if "workspace/memory/reference.md" in stripped:
                return True
    return False


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

from .registry import MonitorRegistry
from .run_records import RunTracker, _parse_iso
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

# Most failed publishes one monitor parks for retry (#1006). A detector that
# reports hundreds of conditions into a dead event server would otherwise grow
# monitor_state.json without bound and hand the drain hundreds of posts. The
# payloads already parked win: they are the ones that have been waiting.
PARK_MAX_ITEMS = 50

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


def _is_json_payload(data) -> bool:
    """Whether a payload survives the strict JSON the event server speaks.

    Python's json accepts NaN/Infinity and the wire does not, so a payload
    carrying one publishes False forever - worth knowing before parking it
    for a retry that can never succeed.
    """
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _append_manager_log(message: str) -> None:
    try:
        from bobi import paths
        log_path = paths.state_dir() / "manager.log"
        with open(log_path, "a") as lf:
            lf.write(message.rstrip() + "\n")
    except Exception:
        pass


# The bundled check runner whose whole point is WHICH runtime served a tick —
# a $0 cached script or a paid agent regeneration. Recorded per run.
SCRIPT_CACHE_CHECK = "script_cache"


def _run_flavor(monitor) -> str:
    """How this monitor detects — the run record's `flavor`."""
    if monitor.notify:
        return "notify"
    if monitor.command:
        return "command"
    if monitor.check:
        return f"check:{monitor.check}"
    if monitor.sleep_cycle:
        return "sleep_cycle"
    return "description"


def _script_cache_mode(monitor_name: str) -> str:
    """The script-cache runner's mode for the tick it just ran, or "".

    Read back from the runner's own state file rather than plumbed through the
    check protocol: checks return conditions, and widening that contract for
    one runner's telemetry would touch every check.
    """
    try:
        from bobi.monitors import script_cache_checks

        state = script_cache_checks._load_trusted_state(monitor_name)
        return str((state or {}).get("last_mode") or "")
    except Exception:
        log.debug("Could not read script_cache mode for %s", monitor_name,
                  exc_info=True)
        return ""


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
                         on_result, cleanup=None, publish=None,
                         on_error=None) -> None:
    """Launch an out-of-band monitor agent subprocess and report its result.

    The shared subprocess machinery for the check, gate, and curator flavors:
    Popen (never blocking the scheduler thread) plus a waiter thread that
    tees stdout to manager.log and hands ``parse(stdout)`` - or None on
    spawn/timeout failure, indeterminate - to ``on_result``. ``cleanup`` (if
    given) runs exactly once when the subprocess is finished with its inputs.

    ``on_error(reason, detail)``, when given, is called just before
    ``on_result(None)`` on every failure path. ``on_result(None)`` already
    says "this run produced nothing trustworthy"; ``on_error`` carries WHY,
    so the run record can say `spawn-failed: ...` rather than a bare failure.
    """
    from bobi import paths
    root = paths.bobi_root()
    log_path = paths.state_dir() / "manager.log"

    def _note(reason: str, detail: str) -> None:
        if on_error is None:
            return
        try:
            on_error(reason, detail)
        except Exception:  # observability must never break a monitor run
            log.exception("on_error hook failed for monitor %s", monitor_name)

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
        _note("spawn-failed", detail)
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
        _note("spawn-failed", detail)
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
            _note("timeout", detail)
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
            _note("indeterminate-result", detail)
        on_result(result)

    threading.Thread(target=_wait, daemon=True,
                     name=f"{kind}-wait-{monitor_name}").start()


def _monitor_agent_role(monitor) -> str:
    """Resolve the role for an out-of-band monitor agent launch (#212, #695).

    `subagents launch` requires --role. Monitors rarely set one, so fall back
    to the team's entry-point role via Config.entry_role - the same resolution
    named start uses, "manager" included, so a monitor never fails on a config
    that `bobi start` accepts.
    """
    role = getattr(monitor, "role", "") or ""
    if role:
        return role
    try:
        from bobi.config import Config
        from bobi.paths import bound_root
        return Config.load(bound_root()).entry_role
    except Exception:
        log.exception("Failed to resolve entry role for monitor %s",
                      getattr(monitor, "name", "?"))
        return "manager"


def _default_spawn_check(monitor, cwd: str | None, on_verdict,
                         publish=None, on_error=None) -> None:
    """Launch a non-interactive check subprocess and report its verdict.

    Runs `bobi agent <name> subagents launch --as-check` out-of-band so the
    scheduler thread is never blocked, captures the check's stdout, and hands
    the trailing verdict JSON (or None) to ``on_verdict`` from a waiter thread
    when the process exits. The check agent only observes — converting the
    verdict to conditions, dedup, and publishing all happen in the scheduler.
    """
    role = _monitor_agent_role(monitor)

    from bobi import paths
    root = paths.bobi_root()
    cmd = [
        sys.executable, "-m", "bobi.cli",
        "agent", paths.agent_name_for_root(root), "subagents", "launch",
        "-w", "adhoc",
        "--role", role,
        "--non-interactive",
        "--as-check",
        "--task", monitor.description or monitor.name,
    ]
    _spawn_monitor_agent(
        cmd, monitor.name, "check", _parse_verdict, on_verdict,
        publish=publish, on_error=on_error)


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
    """Write a rendered sleep-cycle task to disk and return its absolute path.

    Sleep-cycle prompts include transcript windows up to hundreds of KB, which
    can exceed Linux's per-argv-string limit if passed inline. Store the full
    task under state/sleep-cycle and pass the agent a short request path.
    """
    from bobi import paths

    tasks_dir = paths.state_dir() / "sleep-cycle"
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
        log.error("Failed to write sleep-cycle task for monitor %s: %s",
                  monitor.name, e)
        return None
    return task_path


def _default_spawn_gate(monitor, cwd: str | None, items: list, on_verdict,
                        publish=None, on_error=None) -> None:
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
        if on_error is not None:
            on_error("spawn-failed", "failed to write the gate request file")
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
        publish=publish, on_error=on_error,
    )


def _default_spawn_sleep_cycle(monitor, cwd: str | None, task: str, on_result,
                               publish=None, on_error=None) -> None:
    """Launch the out-of-band sleep-cycle agent with a pre-rendered task (#456).

    Mirrors _default_spawn_gate: the full rendered task rides in a request
    file under run/state/sleep-cycle/ (it can exceed argv limits) and the child
    runs the dedicated `monitors curator` command - NOT the `subagents launch
    --as-check` harness, whose check-verdict semantics would swallow the
    summary (#695). The agent WRITES long_term_memory.md and prints a JSON
    summary; the waiter thread hands the parsed summary (or None) to
    ``on_result`` when the process exits. The scheduler - not the agent -
    owns the cursor advance and the publish.
    """
    from bobi import paths

    task_path = _write_curator_task(monitor, task)
    if task_path is None:
        _publish_monitor_error(
            monitor.name, "sleep-cycle", "spawn-failed",
            "failed to write sleep-cycle task file", publish=publish)
        if on_error is not None:
            on_error("spawn-failed", "failed to write sleep-cycle task file")
        on_result(None)
        return

    root = paths.bobi_root()
    cmd = [
        sys.executable, "-m", "bobi.cli",
        "agent", paths.agent_name_for_root(root), "monitors", "curator",
        "--request", str(task_path),
    ]

    def _parse_sleep_cycle(out: str):
        from bobi.monitors import sleep_cycle as sleep_cycle_mod
        return sleep_cycle_mod.parse_result(out)

    _spawn_monitor_agent(
        cmd, monitor.name, "sleep-cycle", _parse_sleep_cycle, on_result,
        cleanup=lambda: Path(task_path).unlink(missing_ok=True),
        publish=publish, on_error=on_error,
    )


class MonitorScheduler:
    def __init__(self, publish=None, state_path: Path | None = None,
                 now=None, registry_loader=None, spawn_check=None,
                 project_path: Path | None = None, spawn_sleep_cycle=None,
                 spawn_gate=None):
        self.publish = publish or _default_publish
        # The injectable spawn hooks keep their 3-/4-argument signatures so
        # test doubles stay unchanged; the run-record error sink is resolved
        # here, at spawn time, on the scheduler thread — so it always binds
        # the firing that is actually being dispatched.
        self.spawn_check = spawn_check or (
            lambda monitor, cwd, on_verdict: _default_spawn_check(
                monitor, cwd, on_verdict, publish=self.publish,
                on_error=self._run_error_sink(monitor))
        )
        self.spawn_sleep_cycle = spawn_sleep_cycle or (
            lambda monitor, cwd, task, on_result: _default_spawn_sleep_cycle(
                monitor, cwd, task, on_result, publish=self.publish,
                on_error=self._run_error_sink(monitor))
        )
        self.spawn_gate = spawn_gate or (
            lambda monitor, cwd, items, on_verdict: _default_spawn_gate(
                monitor, cwd, items, on_verdict, publish=self.publish,
                on_error=self._run_error_sink(monitor))
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
        # Native checks that declare themselves out-of-band and are still
        # running. In-memory like _gates_in_flight: a manager restart
        # mid-check recorded nothing, so re-running it is safe.
        self._checks_in_flight: set[str] = set()
        # Consecutive indeterminate gate verdicts per monitor, to surface a
        # systematically broken gate (each retry pays for a model call that
        # never lands a verdict). Reset on any successful verdict.
        self._gate_failures: dict[str, int] = {}
        # The firing currently being dispatched, per monitor — how a spawn
        # hook finds the run record it should annotate. Set for the duration
        # of run_monitor's synchronous portion only; the out-of-band
        # callbacks capture their tracker directly.
        self._open_runs: dict[str, RunTracker] = {}
        # Which monitor the retry drain starts from. In-memory: it only has to
        # differ between consecutive ticks, and a restart starting over at 0 is
        # indistinguishable from the drain's first tick.
        self._drain_cursor = 0
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
        """Run every monitor that is currently due, then retry anything parked.

        The drain runs LAST. Its staleness bound lives inside a firing
        (:meth:`_retire_parked`), so a drain that went first would publish a
        stale payload the firing right behind it was about to give up on - a
        manager that comes back on Thursday would send Monday's standup nudge
        and Thursday's, on the same topic, in the same tick.
        """
        registry = self._registry_loader()
        now = self._now()
        for monitor in registry.effective_monitors():
            if self._due(monitor, now):
                self.run_monitor(monitor, registry, now)
        self._drain_parked(registry)

    def _drain_parked(self, registry: MonitorRegistry | None = None) -> None:
        """Retry every parked publish, whether or not its monitor is due (#1006).

        A retry needs the payload, not the detector, the model, or the
        schedule - so this runs off the tick clock and touches none of them.
        That is the whole fix: `_reconcile`'s promise of a retry "next
        interval" was only ever true for a standing condition an interval
        monitor re-derives every tick. For a scheduled monitor the next
        interval is a day away, and a drain that waits for the monitor to be
        due is a drain that never runs.

        The first failed post ends the drain for this tick. One failure means
        the transport is down, and there is nothing to learn from spending a
        10s timeout on each remaining payload - the next tick tries again.
        Which monitor goes first rotates, because that reasoning is only
        usually right: a payload the server rejects outright (a 4xx it will
        never accept) is not the transport, and in a fixed order it would
        starve every monitor behind it forever.

        The state read never WAITS for the lock. `_reconcile` still publishes
        while holding it, so during an outage a check-waiter thread can hold
        it for seconds per failing post - and a drain that queued behind that
        would be stalled by exactly the outage it exists to survive. Skipping
        costs one tick; the payload is durable and is still there.
        """
        registry = registry or self._registry_loader()
        monitors = list(registry.effective_monitors())
        if not monitors:
            # Nothing to drain, and nothing to prune against either: a
            # registry that failed to load must not read as "every monitor was
            # deleted" and take every park with it.
            return
        if not self._state_lock.acquire(blocking=False):
            log.debug("Monitor state lock busy - draining next tick")
            return
        try:
            self._prune_orphan_parks({m.state_key for m in monitors})
            parks = {m.state_key: dict((self.state.get(m.state_key) or {})
                                       .get("pending_publish") or {})
                     for m in monitors}
        finally:
            self._state_lock.release()

        turn = self._drain_cursor
        self._drain_cursor += 1
        start = turn % len(monitors)
        for monitor in monitors[start:] + monitors[:start]:
            items = list((parks.get(monitor.state_key) or {}).items())
            if not items:
                continue
            # Rotate WITHIN the park as well as across monitors. The batch
            # stops at its first failure, so a payload the server rejects
            # outright would otherwise sit at the head forever and blackhole
            # every sibling behind it - on a healthy bus.
            off = turn % len(items)
            items = items[off:] + items[:off]
            log.info("Monitor %s: retrying %d parked publish(es)",
                     monitor.name, len(items))
            tracker = RunTracker(monitor.name, flavor=_run_flavor(monitor))
            drained = self._publish_and_park(
                monitor, [Condition(key=k, data=d) for k, d in items],
                park_new=False)
            if drained < len(items):
                return  # the transport is down - stop the drain for this tick
            # One row per completed recovery, not one per tick: at
            # RETENTION_PER_MONITOR rows, a flapping transport delivering one
            # payload a tick would evict the failed firing this row exists to
            # explain.
            tracker.add_published(drained)
            tracker.close(reason=f"delivered {drained} parked finding(s) "
                                 "from an earlier firing")

    def _prune_orphan_parks(self, known: set) -> None:
        """Drop parks belonging to monitors the registry no longer offers.

        A paused or deleted monitor is never drained (the drain walks
        `effective_monitors`) and never retired (retirement happens inside a
        firing), so without this its park waits indefinitely and is replayed
        as a live finding the moment the monitor comes back. Callers hold
        ``_state_lock`` and have already established that `known` is a real
        registry rather than a failed load.
        """
        pruned = False
        for state_key, entry in self.state.items():
            if state_key in known or not isinstance(entry, dict):
                continue
            orphaned = entry.pop("pending_publish", None)
            if orphaned:
                pruned = True
                log.warning("Monitor %s is no longer registered - giving up on "
                            "%d parked publish(es) (%s)", state_key,
                            len(orphaned), ", ".join(orphaned))
        if pruned:
            self._save_state()

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

        Every firing leaves one run record behind. In-thread flavors close it
        here; the out-of-band ones hand their tracker to the callback that
        resolves them, so the record lands when the firing actually finishes,
        not when it was dispatched.
        """
        if monitor.state_key in self._checks_in_flight:
            # A previous firing's out-of-band check has not finished. Return
            # before opening a run record: this tick did no work, and a row
            # per skipped tick would bury the one that is actually running.
            # `last_run` stays untouched so an `at:` slot is not marked fired.
            log.debug(f"Monitor {monitor.name}: check still in flight — "
                      "skipping this tick")
            return

        sleep_cycle_spawned = False
        tracker = RunTracker(monitor.name, flavor=_run_flavor(monitor))
        self._open_runs[monitor.state_key] = tracker
        try:
            in_flight = False
            if monitor.notify:
                # Scheduled notification — keyed to the due time, so the shared
                # dedup path never suppresses it.
                conditions: list | None = [
                    Condition(key=now.isoformat(),
                              data={"description": monitor.description})
                ]
            elif monitor.command:
                conditions = self._command_conditions(monitor, tracker)
            elif monitor.check and self._check_is_out_of_band(monitor):
                # An agent-invoking runner detects off-thread and reconciles
                # from there, exactly like the description-only flavor.
                self._spawn_native_check(monitor, registry, tracker)
                conditions = None
                in_flight = True
            elif monitor.check:
                conditions = self._check_conditions(monitor, registry, tracker)
            elif monitor.sleep_cycle:
                # The sleep cycle writes an artifact, not a verdict — it does not flow
                # through _reconcile. The cursor advance + publish happen on result.
                sleep_cycle_spawned = self._spawn_sleep_cycle(
                    monitor, registry.projects_for(monitor), tracker)
                conditions = None
                in_flight = sleep_cycle_spawned
            else:
                self._spawn_check(monitor, registry.projects_for(monitor),
                                  tracker)
                conditions = None  # detection in flight — reconciled on verdict
                in_flight = True

            if conditions is not None:
                if monitor.gated:
                    # Two-tier semantic gate (#630): the mechanical detector
                    # decided what exists, a cheap-model gate judges what is new.
                    in_flight = self._reconcile_gated(
                        monitor, conditions, registry.projects_for(monitor),
                        tracker)
                else:
                    self._reconcile(monitor, conditions, tracker)

            if not in_flight:
                # Detection resolved in this thread: either it produced an
                # answer (close on what was published) or the detector failed
                # and noted why (close as failed).
                tracker.close()
        finally:
            self._open_runs.pop(monitor.state_key, None)

        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            entry["last_run"] = now.isoformat()
            if monitor.sleep_cycle and sleep_cycle_spawned:
                entry["last_spawn"] = now.isoformat()
            self._save_state()

    def _sleep_cycle_error(self, monitor, reason: str, detail: str,
                           tracker=None) -> None:
        """Publish a sleep-cycle failure and record it against the firing.

        The sleep cycle has many ways to turn back (unreadable artifact, blown
        memory budget, a reference file that did not change); each one already
        published `system/monitor.error`. Routing them through here means the
        run record carries the same reason the bus event did.
        """
        _publish_monitor_error(monitor.name, "sleep-cycle", reason, detail,
                               publish=self.publish)
        if tracker is not None:
            tracker.note_failure(f"{reason}: {detail}" if detail else reason)

    def _sleep_cycle_turn_back(self, monitor, reason: str, detail: str,
                               tracker=None) -> None:
        """Log and record a validation that turned the sleep cycle back.

        The caller returns immediately after, leaving the cursor where it was
        so the same transcript delta is retried next interval — the shared
        shape of every artifact/budget check in
        :meth:`_apply_sleep_cycle_result`. The log line lives here so the next
        validation gate is one call, not another copy of it.

        Not every :meth:`_sleep_cycle_error` caller turns back this way: the
        KB-sync paths log without the retry note (or with it conditionally),
        so the line stays out of that method.
        """
        log.warning("Monitor %s: %s - cursor NOT advanced, retrying next interval",
                    monitor.name, detail)
        self._sleep_cycle_error(monitor, reason, detail, tracker)

    def _run_error_sink(self, monitor):
        """A `(reason, detail)` sink bound to the firing being dispatched.

        Resolved when a spawn hook runs — synchronously, on the scheduler
        thread, inside run_monitor — so it can never bind a later firing's
        record. Returns None when there is no open run (a direct call from a
        test, say), which the spawn helpers treat as "nothing to annotate".
        """
        tracker = self._open_runs.get(monitor.state_key)
        if tracker is None:
            return None

        def _sink(reason: str, detail: str = "") -> None:
            tracker.note_failure(f"{reason}: {detail}" if detail else reason)

        return _sink

    def _reconcile(self, monitor, conditions: list, tracker=None) -> None:
        """The single dedup + publish chokepoint for every monitor flavor.

        Fires events only for conditions that weren't active last time.
        Conditions that disappeared drop out; if they recur later they fire
        again. A still-present condition is never re-fired on the next
        interval.

        A new condition is recorded active only once its event actually
        published. A publish that failed is parked with its payload and
        retried by the tick drain (#1006) - the park owns that key from then
        on, so a key sitting in it is skipped here rather than re-fired. One
        outage must not deliver the same finding twice.
        """
        with self._state_lock:
            entry = self.state.setdefault(monitor.state_key, {})
            previous = set(entry.get("active", []))
            parked = set(entry.get("pending_publish") or {})
            current = {c.key: c for c in conditions}
            entry["active"] = [k for k in current if k in previous]
            # A key sitting in the park is neither active nor new: the drain
            # owns it until it lands.
            held = [k for k in current if k in parked]
            new = [current[k] for k in current
                   if k not in previous and k not in parked]
            retired = self._retire_parked(monitor, entry, set(current))
            outcomes = [self._record_publish_outcome(
                            monitor, c, self._fire(monitor, c)) for c in new]
            self._save_state()
        # This firing's own outcomes first: `note_failure` keeps the first
        # reason, and the two below belong to an earlier firing.
        self._note_publish_outcomes(tracker, outcomes)
        if tracker is not None and retired:
            tracker.note_failure(
                f"{len(retired)} finding(s) parked by an earlier firing were "
                "never published and are no longer detected - dropped")
        if tracker is not None and held:
            # Without this the firing closes `quiet` while a finding nobody
            # has heard sits in the park - a monitor that cannot deliver would
            # read as healthy in the runs view.
            tracker.note_failure(
                f"{len(held)} finding(s) parked by an earlier firing are "
                "still undelivered")

    # What one publish attempt did, as recorded. `dropped` is a give-up: the
    # payload is gone and nothing will retry it.
    PUBLISHED, PARKED, DROPPED = "published", "parked", "dropped"

    def _record_publish_outcome(self, monitor, condition, ok: bool, *,
                                park_new: bool = True) -> str:
        """Fold one publish attempt into the monitor's state entry.

        The single place that decides what a publish attempt MEANS, shared by
        every publisher. Published: recorded active, unparked. Failed: parked
        in ``pending_publish`` with its payload, which is everything a retry
        needs - unless it is a give-up, which is never reported as a park,
        because saying "retrying" over a finding nothing retries is the whole
        of #1006.

        ``park_new=False`` restricts the write to keys already in the park:
        the drain publishes outside the lock, and a key a concurrent firing
        retired mid-post must stay retired rather than be resurrected by the
        post that was already in flight.

        Callers hold ``_state_lock`` and save the state themselves - this
        folds several attempts into one write.
        """
        entry = self.state.setdefault(monitor.state_key, {})
        pending = dict(entry.get("pending_publish") or {})
        key = condition.key
        result = self.PARKED
        if ok:
            pending.pop(key, None)
            active = entry.setdefault("active", [])
            if key not in active:
                active.append(key)
            result = self.PUBLISHED
        elif key in pending:
            pending[key] = condition.data
        elif not park_new:
            result = self.DROPPED  # retired while this post was in flight
        elif not _is_json_payload(condition.data):
            # The wire is strict JSON and the server rejects anything else, so
            # no number of retries can land this one (a command monitor
            # printing NaN, say). Parking it would also make monitor_state.json
            # unreadable to every non-Python reader.
            log.error("Monitor %s: finding %s carries a payload the event "
                      "server can never accept - not parked, lost",
                      monitor.name, key)
            result = self.DROPPED
        elif len(pending) < PARK_MAX_ITEMS:
            pending[key] = condition.data
        else:
            log.error("Monitor %s: publish failed and the retry park is full "
                      "(%d) - finding %s is lost", monitor.name,
                      PARK_MAX_ITEMS, key)
            result = self.DROPPED
        self._save_park(entry, pending)
        return result

    @staticmethod
    def _save_park(entry: dict, pending: dict) -> None:
        """Write a monitor's park back, dropping the key when it is empty."""
        if pending:
            entry["pending_publish"] = pending
        else:
            entry.pop("pending_publish", None)

    def _note_publish_outcomes(self, tracker, outcomes: list) -> None:
        """Tell the firing's run record what its publishes did.

        The failure nearest the root cause wins (`note_failure` keeps the
        first), so a give-up is recorded ahead of a park: one says a finding
        is gone, the other says it is queued.
        """
        if tracker is None:
            return
        tracker.add_published(outcomes.count(self.PUBLISHED))
        dropped = outcomes.count(self.DROPPED)
        parked = outcomes.count(self.PARKED)
        if dropped:
            tracker.note_failure(
                f"{dropped} finding(s) failed to publish and were DROPPED - "
                "nothing will retry them (see manager.log)")
        if parked:
            # Nobody heard about these findings - the firing did not
            # deliver, however many of its siblings did.
            tracker.note_failure(
                f"{parked} finding(s) failed to publish - "
                "parked for a mechanical retry")

    def _retire_parked(self, monitor, entry: dict, keep: set) -> list:
        """Drop parked payloads the detector no longer reports. Returns them.

        A parked key waits for the transport, not forever. The bound is the
        monitor's own next firing: a standing condition keeps re-appearing in
        `keep` and keeps its place in the park, while a key that firing no
        longer produces has been retired at the source - a scheduled monitor's
        keys are date- or instant-stamped, so its park lasts exactly until the
        next slot. Delivering Monday's standup nudge on Tuesday would be worse
        than not delivering it.

        Callers hold ``_state_lock``.
        """
        pending = dict(entry.get("pending_publish") or {})
        retired = [k for k in pending if k not in keep]
        if not retired:
            return retired
        for key in retired:
            pending.pop(key)
        self._save_park(entry, pending)
        log.warning("Monitor %s: giving up on %d parked publish(es) the "
                    "detector no longer reports (%s)", monitor.name,
                    len(retired), ", ".join(retired))
        return retired

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
            # What happens to it next is the caller's decision (park or give
            # up), and it is logged there - this line must not promise a
            # retry it does not make.
            log.warning(f"Monitor {monitor.name} failed to publish {event} "
                        f"({condition.key})")
        return ok

    # --- relevance gate (two-tier semantic gate, #630) -------------------

    def _reconcile_gated(self, monitor, conditions: list,
                         projects: list[Path], tracker=None) -> bool:
        """Dedup first, then judge ONLY the new conditions with a cheap-model
        relevance gate before anything publishes.

        Returns True when a gate is in flight — the firing is not finished and
        its run record stays open until the verdict lands.

        The cost shape is the point: a tick where the mechanical detector
        finds nothing new ends here with zero LLM calls (the common case).
        Only genuinely new keys ride to an out-of-band gate agent; the
        verdict callback publishes the relevant ones and records the
        irrelevant ones active WITHOUT publishing, so each item is judged
        exactly once. A judged-relevant item whose publish failed sits in
        ``pending_publish`` and is retried mechanically at $0 by the tick
        drain - never re-sent to the model, whose second opinion on a
        borderline item could flip and silently drop the finding. Clearing
        semantics match _reconcile for ACTIVE keys: disappeared keys drop out
        and re-fire if they recur.

        The park is the one place the two paths differ, deliberately. A gated
        park is NOT retired when the detector stops reporting its key
        (:meth:`_retire_parked`, which _reconcile applies): re-detection here
        costs a model call, so dropping a judged payload means either paying
        to judge the same item twice or - for a window-scoped detector whose
        item ages out - losing a finding that was already judged worth
        sending. It is held until it lands.
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

        if not spawn:
            return False
        cwd = str(projects[0]) if projects else None
        log.info(f"Monitor {monitor.name}: gating {len(new)} new item(s) "
                 "against its relevance criterion")
        try:
            self.spawn_gate(monitor, cwd, new,
                            lambda verdict: self._on_gate_verdict(
                                monitor, new, verdict, tracker))
        except Exception as e:
            # A spawn that raised will never deliver a verdict - lift the
            # in-flight guard so the next tick can retry.
            with self._state_lock:
                self._gates_in_flight.discard(monitor.state_key)
            log.error(f"Failed to spawn gate for monitor {monitor.name}: {e}")
            if tracker is not None:
                tracker.note_failure(f"gate spawn failed: {e}")
            return False
        return True

    def _publish_and_park(self, monitor, conditions: list, tracker=None, *,
                          park_new: bool = True) -> int:
        """Publish a batch and park whatever did not land. Returns how many did.

        Serves both publishers that already hold a payload rather than a
        detection: the relevance gate's judged-relevant items, and the tick
        drain retrying a park.

        Publishes run OUTSIDE the state lock - they are HTTP posts and must
        never stall the scheduler tick or the check-waiter threads queued on
        the lock. The first failure stops the batch: one failed post means
        the transport is down, so the rest are parked unattempted instead of
        spending a 10s timeout each proving the same thing.
        """
        attempts, up = [], True
        for condition in conditions:
            if up:
                up = self._fire(monitor, condition)
            attempts.append((condition, up))
        with self._state_lock:
            outcomes = [self._record_publish_outcome(monitor, c, ok,
                                                    park_new=park_new)
                        for c, ok in attempts]
            self._save_state()
        self._note_publish_outcomes(tracker, outcomes)
        return outcomes.count(self.PUBLISHED)

    def _on_gate_verdict(self, monitor, judged: list,
                         verdict: dict | None, tracker=None) -> None:
        """Reconcile a relevance-gate verdict (waiter-thread callback).

        Irrelevant items are recorded active without publishing - judged
        once, never re-judged. Relevant items publish via _publish_and_park;
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
            if tracker is not None:
                tracker.note_failure("relevance gate returned no verdict — "
                                     "new items stay unjudged")
                tracker.close()
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
            self._publish_and_park(monitor, to_publish, tracker)

        with self._state_lock:
            self._gates_in_flight.discard(key)
        if tracker is not None:
            tracker.close()

    # --- detectors ------------------------------------------------------

    def _check_is_out_of_band(self, monitor) -> bool:
        """Whether this monitor's native check must not run on this thread.

        A runner opts in by setting ``out_of_band = True`` on itself — the
        same `*_checks.py` glob loads framework and pack runners, so a pack
        that pays for an agent can declare it without a scheduler change.
        """
        return bool(getattr(self._checks.get(monitor.check), "out_of_band", False))

    def _spawn_native_check(self, monitor, registry: MonitorRegistry,
                            tracker=None) -> None:
        """Run a native check on its own thread and reconcile from there.

        The scheduler has ONE thread: `_loop` -> `tick` -> `run_monitor`, and
        `tick` cannot return until every due monitor's detection has. A
        runner that calls the agent runtime (script_cache's self-heal, via
        `run_check_blocking` — attempts=2 x CHECK_TIMEOUT=600s) therefore
        stalled every OTHER monitor for up to ~20 minutes: interval monitors
        drifted, and a weekday-gated `at:` slot whose instant passed during
        the block was read as a missed-while-down catch-up and skipped
        outright (D004). The description-only flavor already detects
        out-of-band for exactly this reason; this gives native runners the
        same seam, and reconciliation takes the same state lock the
        check-waiter threads do.
        """
        key = monitor.state_key
        self._checks_in_flight.add(key)
        projects = registry.projects_for(monitor)

        def _detect() -> None:
            try:
                conditions = self._check_conditions(monitor, registry, tracker)
                if conditions is None:
                    log.warning(f"Monitor {monitor.name}: check indeterminate "
                                "— leaving state untouched, retrying next "
                                "interval")
                elif monitor.gated:
                    if self._reconcile_gated(monitor, conditions, projects,
                                             tracker):
                        return  # a gate is in flight; it closes the record
                else:
                    self._reconcile(monitor, conditions, tracker)
            except Exception as e:  # a worker thread must never die silently
                log.error(f"Monitor {monitor.name}: out-of-band check "
                          f"failed: {e}")
                if tracker is not None:
                    tracker.note_failure(f"check raised: {e}")
            finally:
                self._checks_in_flight.discard(key)
            if tracker is not None:
                tracker.close()

        threading.Thread(target=_detect, daemon=True,
                         name=f"monitor-check-{monitor.name}").start()

    def _check_conditions(self, monitor, registry: MonitorRegistry,
                          tracker=None) -> list | None:
        """Run a native check runner. None when the check itself failed."""
        check = self._checks.get(monitor.check)
        if check is None:
            reason = f"unknown check '{monitor.check}'"
            log.warning(f"Monitor {monitor.name} names {reason} — skipping")
            if tracker is not None:
                tracker.note_failure(reason)
            return None
        try:
            return check(monitor, registry.projects_for(monitor))
        except Exception as e:
            log.error(f"Check '{monitor.check}' for {monitor.name} failed: {e}")
            if tracker is not None:
                tracker.note_failure(f"check '{monitor.check}' raised: {e}")
            return None
        finally:
            if tracker is not None and monitor.check == SCRIPT_CACHE_CHECK:
                # Whether this tick ran the $0 cached script or paid an agent
                # to regenerate it is the script-cache runner's whole story.
                tracker.note_script_cache_mode(_script_cache_mode(monitor.name))

    def _command_conditions(self, monitor, tracker=None) -> list | None:
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
            if tracker is not None:
                tracker.note_failure("command timed out after 60s")
            return None
        except OSError as e:
            log.error(f"Command monitor {monitor.name} failed to run: {e}")
            if tracker is not None:
                tracker.note_failure(f"command failed to run: {e}")
            return None

        if result.returncode != 0:
            stderr = result.stderr.strip()[:200] if result.stderr else ""
            log.warning(f"Command monitor {monitor.name} exited {result.returncode}: {stderr}")
            if tracker is not None:
                tracker.note_failure(
                    f"command exited {result.returncode}"
                    + (f": {stderr}" if stderr else ""))
            return None

        stdout = result.stdout.strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            log.warning(f"Command monitor {monitor.name} returned non-JSON output")
            if tracker is not None:
                tracker.note_failure("command printed non-JSON output")
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

    def _spawn_check(self, monitor, projects: list[Path], tracker=None) -> None:
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
                         lambda verdict: self._on_check_verdict(
                             monitor, verdict, tracker))

    def _on_check_verdict(self, monitor, verdict: dict | None,
                          tracker=None) -> None:
        """Reconcile an out-of-band check's verdict (waiter-thread callback)."""
        if tracker is not None and isinstance(verdict, dict):
            # The check agent reports the session it ran under, so its run
            # row can open the transcript rather than a bare "no detail".
            tracker.note_session(str(verdict.get("session", "")))
        conditions = self._verdict_conditions(verdict)
        if conditions is None:
            log.warning(f"Monitor {monitor.name}: check indeterminate — "
                        "leaving state untouched, retrying next interval")
            if tracker is not None:
                tracker.note_failure("check agent returned no usable verdict")
                tracker.close()
            return
        self._reconcile(monitor, conditions, tracker)
        if tracker is not None:
            tracker.close()

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

    # --- sleep-cycle flavor (#456) -------------------------------------

    def _project_root(self, projects: list[Path]):
        """The bound project root for path resolution — the first applicable
        project, or the scheduler's own bound root."""
        if projects:
            return projects[0]
        return self._project_path

    def _load_sleep_cycle_prompt(self, root) -> str:
        """The sleep-cycle agent's working instructions. Team override first
        (<run>/package/prompts/sleep_cycle.md), then the legacy
        <run>/package/prompts/curator.md override, framework default otherwise -
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
            log.error("Sleep-cycle prompt missing at %s", SLEEP_CYCLE_PATH)
            return ""

    def _spawn_sleep_cycle(self, monitor, projects: list[Path],
                           tracker=None) -> bool:
        """Window the transcript delta, apply the input cap, and launch the
        sleep-cycle agent with the rendered delta (#456).

        The scheduler owns the deterministic half - read the success-advanced
        cursor, index new transcript lines, select messages oldest-first by id
        under MAX_SLEEP_CYCLE_INPUT_CHARS (deferring the overflow, truncating an
        oversized oldest message) - so the cursor / cap / no-silent-skip
        invariants live in plain code, never behind the model. The agent does
        the judgment and rewrites long_term_memory.md; _on_sleep_cycle_result advances the
        cursor and publishes on success.
        """
        from bobi import history, paths
        from bobi.memory import (
            MAX_MEMORY_CHARS,
            WORKING_MEMORY_CHARS,
            collect_legacy_journals,
            load_raw_long_term_memory,
        )
        from bobi.monitors import sleep_cycle as sleep_cycle_mod

        root = self._project_root(projects)
        paths.migrate_long_term_memory_state(root)
        state_dir = paths.state_path(root)
        memory_path = paths.long_term_memory_path(root)
        reference_path = paths.workspace_dir(root) / "memory" / "reference.md"
        cursor_path = paths.long_term_memory_cursor_path(root)
        cursor = sleep_cycle_mod.read_cursor(cursor_path)

        try:
            history.index()  # incremental - only new JSONL lines
        except Exception as e:
            log.warning("Sleep-cycle transcript index failed for %s: %s", monitor.name, e)

        rows = history.messages_since(cursor)

        # One-time seed (#456): on the very first run (no long_term_memory.md yet) distill
        # the existing per-session decision-log journals into the first long_term_memory.md
        # so accumulated knowledge isn't discarded at rollout. Guarded on
        # long_term_memory.md absence -> idempotent: once written, the seed never re-fires.
        seed = ""
        if not memory_path.is_file():
            seed = collect_legacy_journals(state_dir, sleep_cycle_mod.MAX_SEED_INPUT_CHARS)

        try:
            current_memory = load_raw_long_term_memory(state_dir)
        except Exception:
            current_memory = ""
        try:
            current_reference = reference_path.read_text() if reference_path.is_file() else ""
        except OSError:
            current_reference = ""
        memory_chars = len(current_memory)
        compaction_required = memory_chars > WORKING_MEMORY_CHARS

        if not rows and not seed and not compaction_required:
            try:
                from bobi.memory import cold_memory_kb_needs_sync

                if cold_memory_kb_needs_sync(root, reference_path):
                    self._sync_reference_kb_or_publish_error(
                        monitor, root, reference_path, demoted=0,
                        reference_updated=False, tracker=tracker)
            except Exception as e:
                detail = (
                    f"workspace/memory/reference.md KB sync check failed at "
                    f"{reference_path}: {e}"
                )
                log.warning("Monitor %s: %s", monitor.name, detail)
                self._sleep_cycle_error(
                    monitor, "reference-kb-index-failed", detail, tracker)
            log.info("Monitor %s due - no new transcript messages since cursor %d "
                     "and nothing to seed", monitor.name, cursor)
            return False

        ingested, highest_id, flags = sleep_cycle_mod.select_messages(
            rows, sleep_cycle_mod.MAX_SLEEP_CYCLE_INPUT_CHARS)
        if highest_id is None and not seed and not compaction_required:
            log.info("Monitor %s: nothing ingestable this run", monitor.name)
            return False

        if compaction_required:
            flags["memory_over_budget"] = True
            flags["memory_over_cap"] = memory_chars > MAX_MEMORY_CHARS
            flags["memory_chars"] = memory_chars
            flags["memory_budget"] = WORKING_MEMORY_CHARS
            flags["memory_cap"] = MAX_MEMORY_CHARS

        transcript = sleep_cycle_mod.render_transcript(ingested)
        task = sleep_cycle_mod.build_sleep_cycle_task(
            self._load_sleep_cycle_prompt(root), transcript, current_memory, flags,
            seed=seed, current_reference=current_reference)
        if seed:
            log.info("Monitor %s: seeding first long_term_memory.md from %d chars of legacy "
                     "journals", monitor.name, len(seed))

        cwd = str(projects[0]) if projects else None
        log.info("Monitor %s due - spawning sleep cycle over %d new message(s) "
                 "(highest id %s, deferred=%s, compaction_required=%s)",
                 monitor.name, len(ingested), highest_id,
                 flags.get("input_truncated"), compaction_required)
        self.spawn_sleep_cycle(
            monitor, cwd, task,
            lambda result: self._on_sleep_cycle_result(
                monitor, result, highest_id, cursor_path, memory_path,
                compaction_required, reference_path,
                hashlib.sha256(current_reference.encode()).hexdigest(),
                tracker),
        )
        return True

    def _spawn_curator(self, monitor, projects: list[Path]) -> bool:
        """Deprecated compatibility wrapper for one release."""
        return self._spawn_sleep_cycle(monitor, projects)

    def _on_sleep_cycle_result(self, monitor, result: dict | None,
                               highest_id: int | None, cursor_path: Path,
                               memory_path: Path | None = None,
                               compaction_required: bool = False,
                               reference_path: Path | None = None,
                               reference_before_hash: str | None = None,
                               tracker=None) -> None:
        """Waiter-thread callback for a finished sleep-cycle run.

        Advances the cursor ONLY on success (a failed/indeterminate run leaves
        it unmoved so the same window is re-read next interval - no transcript
        skipped). Publishes `system/memory.updated` only when the run actually
        changed long_term_memory.md.

        Every exit closes the firing's run record — the reason, when there is
        one, was noted by whichever _sleep_cycle_error call turned back.
        """
        try:
            self._apply_sleep_cycle_result(
                monitor, result, highest_id, cursor_path, memory_path,
                compaction_required, reference_path, reference_before_hash,
                tracker)
        finally:
            if tracker is not None:
                tracker.close()

    def _apply_sleep_cycle_result(self, monitor, result: dict | None,
                                  highest_id: int | None, cursor_path: Path,
                                  memory_path: Path | None,
                                  compaction_required: bool,
                                  reference_path: Path | None,
                                  reference_before_hash: str | None,
                                  tracker) -> None:
        from bobi.memory import MAX_MEMORY_CHARS, WORKING_MEMORY_CHARS
        from bobi.monitors import sleep_cycle as sleep_cycle_mod

        if not isinstance(result, dict):
            log.warning("Monitor %s: sleep-cycle run failed/indeterminate - cursor "
                        "NOT advanced, retrying next interval", monitor.name)
            if tracker is not None:
                tracker.note_failure(
                    "sleep-cycle agent returned no usable result")
            return
        if not result.get("success"):
            summary = str(result.get("summary", "") or "sleep cycle returned failure")
            log.warning("Monitor %s: sleep-cycle run failed - cursor NOT advanced, "
                        "retrying next interval: %s", monitor.name, summary)
            self._sleep_cycle_error(
                monitor, "indeterminate-result", summary, tracker)
            return

        path = memory_path or cursor_path.with_name("long_term_memory.md")
        should_exist = bool(result.get("updated")) or compaction_required
        try:
            if path.is_file():
                actual_chars = len(path.read_text())
            elif should_exist:
                raise FileNotFoundError(path)
            else:
                actual_chars = 0
        except OSError as e:
            detail = (
                f"long_term_memory.md artifact unreadable at {path}: {e}; "
                f"compaction_required={compaction_required}; "
                f"updated={bool(result.get('updated'))}"
            )
            self._sleep_cycle_turn_back(
                monitor, "memory-artifact-unreadable", detail, tracker)
            return
        if actual_chars > MAX_MEMORY_CHARS:
            detail = (
                "long_term_memory.md output exceeded cap: "
                f"{actual_chars} chars (cap {MAX_MEMORY_CHARS}) at {path}; "
                f"compaction_required={compaction_required}; "
                f"updated={bool(result.get('updated'))}"
            )
            self._sleep_cycle_turn_back(
                monitor, "memory-cap-exceeded", detail, tracker)
            return
        if actual_chars > WORKING_MEMORY_CHARS and (
            compaction_required or result.get("updated")
        ):
            detail = (
                "long_term_memory.md output exceeded working budget: "
                f"{actual_chars} chars (budget {WORKING_MEMORY_CHARS}, "
                f"hard cap {MAX_MEMORY_CHARS}) at {path}; "
                f"compaction_required={compaction_required}; "
                f"updated={bool(result.get('updated'))}"
            )
            self._sleep_cycle_turn_back(
                monitor, "memory-working-budget-exceeded", detail, tracker)
            return
        reference_changed_required = bool(
            result.get("demoted") or result.get("reference_updated")
        )
        if reference_changed_required:
            ref_path = reference_path or path.parent.parent / "workspace" / "memory" / "reference.md"
            try:
                reference = ref_path.read_text()
            except OSError as e:
                detail = (
                    f"workspace/memory/reference.md artifact unreadable at {ref_path}: {e}; "
                    f"demoted={result.get('demoted')}"
                )
                self._sleep_cycle_turn_back(
                    monitor, "reference-artifact-unreadable", detail, tracker)
                return
            if not reference.strip() or "## " not in reference:
                detail = (
                    f"workspace/memory/reference.md artifact invalid at {ref_path}; "
                    f"demoted={result.get('demoted')}"
                )
                self._sleep_cycle_turn_back(
                    monitor, "reference-artifact-invalid", detail, tracker)
                return
            after_hash = hashlib.sha256(reference.encode()).hexdigest()
            if reference_before_hash is not None and after_hash == reference_before_hash:
                detail = (
                    f"workspace/memory/reference.md artifact unchanged at {ref_path}; "
                    f"demoted={result.get('demoted', 0)}; "
                    f"reference_updated={bool(result.get('reference_updated'))}"
                )
                self._sleep_cycle_turn_back(
                    monitor, "reference-artifact-unchanged", detail, tracker)
                return
            memory_content = path.read_text() if path.is_file() else ""
            if not _facts_section_has_reference_pointer(memory_content):
                detail = (
                    f"long_term_memory.md missing workspace/memory/reference.md Facts pointer "
                    f"at {path}; "
                    f"demoted={result.get('demoted', 0)}; "
                    f"reference_updated={bool(result.get('reference_updated'))}"
                )
                self._sleep_cycle_turn_back(
                    monitor, "reference-pointer-missing", detail, tracker)
                return
            sync = self._sync_reference_kb_or_publish_error(
                monitor, path.parent.parent, ref_path,
                demoted=result.get("demoted", 0),
                reference_updated=bool(result.get("reference_updated")),
                retry_note=True,
                tracker=tracker,
            )
            if sync is None:
                return
            result["deduped"] = int(sync.get("deduped", 0) or 0)
            result["merged"] = int(sync.get("merged", 0) or 0)
            result["flagged"] = int(sync.get("flagged", 0) or 0)

        # A seed-only first run ingests no transcript rows (highest_id is None) -
        # there is nothing to advance; the cursor stays at 0 and the next run
        # reads the real transcript delta normally.
        if highest_id is not None:
            try:
                sleep_cycle_mod.write_cursor(cursor_path, highest_id)
            except OSError as e:
                log.error("Monitor %s: failed to advance sleep-cycle cursor: %s",
                          monitor.name, e)

        if result.get("lossy_drops"):
            log.warning("Monitor %s: sleep cycle made %s LOSSY drop(s) of still-valid "
                        "items for space — raise MAX_MEMORY_CHARS / build the "
                        "decisions-spill: %s", monitor.name,
                        result.get("lossy_drops"), result.get("summary", ""))

        if result.get("updated"):
            self._publish_memory_updated(monitor, result, tracker)
        else:
            log.info("Monitor %s: sleep cycle found nothing durable - no publish",
                     monitor.name)

    def _sync_reference_kb_or_publish_error(
        self,
        monitor,
        root: Path,
        reference_path: Path,
        *,
        demoted: int = 0,
        reference_updated: bool = False,
        retry_note: bool = False,
        tracker=None,
    ) -> dict | None:
        try:
            from bobi.memory import sync_reference_to_cold_memory_kb

            return sync_reference_to_cold_memory_kb(
                root,
                reference_path,
                source_session=monitor.name,
            )
        except Exception as e:
            detail = (
                f"workspace/memory/reference.md KB indexing failed at {reference_path}: {e}; "
                f"demoted={demoted}; reference_updated={reference_updated}"
            )
            suffix = " - cursor NOT advanced, retrying next interval" if retry_note else ""
            log.warning("Monitor %s: %s%s", monitor.name, detail, suffix)
            self._sleep_cycle_error(
                monitor, "reference-kb-index-failed", detail, tracker)
            return None

    def _publish_memory_updated(self, monitor, result: dict, tracker=None) -> None:
        """Publish the completion event directly (bypassing _reconcile dedup).

        A completion signal is not a deduped finding - two runs with the same
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
        for key in ("deduped", "merged", "flagged"):
            if key in result:
                payload[key] = int(result.get(key, 0) or 0)
        published = False
        published_events = set()
        for candidate in (event, "system/memory.updated", "system/policy.updated"):
            if candidate in published_events:
                continue
            published_events.add(candidate)
            ok = self.publish(candidate, payload)
            if candidate == event:
                published = ok
        if published:
            log.info("Monitor %s published %s (urgent=%s)",
                     monitor.name, event, payload["urgent"])
            if tracker is not None:
                tracker.add_published(1)
        else:
            log.warning("Monitor %s failed to publish %s", monitor.name, event)
            if tracker is not None:
                tracker.note_failure(f"failed to publish {event}")

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
        """Persist last_run/last_spawn atomically.

        ``_load_state`` treats corrupt JSON as 'resetting', so a kill
        mid-write does not lose one monitor's timestamp — it drops the whole
        schedule and every monitor re-fires on the next boot.
        """
        try:
            atomic_write_json(self.state_path, self.state)
        except OSError as e:
            log.warning(f"Failed to persist monitor state: {e}")
