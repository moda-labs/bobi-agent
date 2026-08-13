"""Supervision core — a faithful port of the public self-heal watchdog.

This is a byte-for-byte behavioral port of the deleted ``bobi/watchdog.py``'s
``Supervisor`` / ``RestartBudget`` / ``is_wedged`` (issue #464). It was lifted
out to the private deploy repo when the public watchdog was deleted (issue #5)
and returned here with the sidecar by the repo reorg. The
restart/wedge/crash/backoff/exit-70 state machine is unchanged, and the unit +
acceptance tests have travelled with it the whole way (now back in
``tests/test_supervision*.py``). The one behavioral addition since the port: a
positively-dead director (``status=error``, invisible to every ported recovery
path) restarts immediately through the same budget/backoff machine (#12,
:data:`DEAD_STATES`).

What the sidecar adds on top of the port is strictly *additive* and never
changes a restart decision:

- an injectable :class:`SupervisorObserver`. The supervisor reports each poll
  (for the tier-1 heartbeat) and emits the lifecycle edges it *acts* on
  (``manager_started`` / ``manager_stopped`` / ``manager_restarted`` /
  ``budget_exhausted``). The observed-condition episodes
  (``probe_failing`` / ``probe_recovered``) are derived by the telemetry layer
  from the outside verdict it computes per poll, not here.
- last-restart bookkeeping (reason + timestamp) and the last health payload,
  surfaced to the observer so the heartbeat can report them.
- the load-grace gate (issue #903). When the host is saturated by
  the manager's own busy worker tree, the ambiguous liveness verdicts (probe
  misses, stalled active turns, a load-induced dead-director signal) are
  *deferred* - no charge, no restart - until the evidence ends or the spell
  cap expires. A real child exit never reaches the gate: the crash path above
  stays authoritative. See :meth:`Supervisor._defer_for_load`.

The wedge *decision* still lives in :func:`is_wedged`; process management and
health polling are still injectable so the state machine is unit-testable
without real processes or wall-clock waits.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from bobi import paths

from .config import SupervisorConfig

log = logging.getLogger(__name__)

# The director is only restartable-for-inactivity while it is *making a turn*.
# These are the active states from session.py's status machine; idle/stopped/
# done/error are not active and are never treated as wedged.
ACTIVE_STATES = frozenset({"starting", "running"})

# A *positive* death signal, not uncertainty (#12): `status=error` means the
# director's drain loop raised and nothing else recovers it - the health server
# is a daemon thread that outlives the dead director (so the probe keeps
# answering and `_fail_count` never trips) and the child process stays alive
# (so the crash path never fires). Unknown statuses remain fail-open; only this
# explicit signal routes to an immediate restart.
DEAD_STATES = frozenset({"error"})

# Non-zero exit so the orchestrator's restart policy takes over after the
# supervisor exhausts its in-container restart budget - escalation, not a silent
# park. Orchestrator-neutral: Fly machine restart / K8s CrashLoopBackOff /
# docker --restart all key off the non-zero exit.
EXIT_BUDGET_EXHAUSTED = 70  # EX_SOFTWARE


def is_wedged(status, idle_seconds, stall_threshold: float) -> bool:
    """The wedge discriminator: a *positive* signal only.

    True iff the director is in an active turn state and has made zero registry
    progress for longer than ``stall_threshold``. Any uncertainty (unknown
    status, missing ``idle_seconds``) returns False - uncertainty must never
    trigger a restart (fail-open).
    """
    if status not in ACTIVE_STATES:
        return False
    if idle_seconds is None:
        return False
    try:
        idle = float(idle_seconds)
    except (TypeError, ValueError):
        return False
    # A non-finite idle (NaN, or Infinity - which json.loads accepts and which
    # would otherwise pass `> threshold`) is corrupt input, not a wedge signal:
    # fail open.
    if not math.isfinite(idle):
        return False
    return idle > stall_threshold


class RestartBudget:
    """Windowed restart counter shared by *both* restart paths.

    Wedge-restarts and fast-crash relaunches draw on one budget so neither a
    wedge loop nor a crash loop can run unbounded. Backoff spaces successive
    attempts so even within budget nothing tight-loops.
    """

    def __init__(self, max_restarts: int, window: float,
                 backoff: tuple[float, ...]):
        self.max_restarts = max_restarts
        self.window = window
        self.backoff = backoff
        self._stamps: list[float] = []

    def _recent(self, now: float) -> list[float]:
        return [t for t in self._stamps if now - t < self.window]

    def count(self, now: float) -> int:
        """Restarts inside the current window (prunes aged-out stamps)."""
        self._stamps = self._recent(now)
        return len(self._stamps)

    def exhausted(self, now: float) -> bool:
        """True when another restart would exceed the windowed budget."""
        return self.count(now) >= self.max_restarts

    def record(self, now: float) -> None:
        self._stamps.append(now)

    def reset(self) -> None:
        """Clear the restart history. An OPERATOR restart is intentional, not a
        crash/wedge, so it must not draw down (or trip) the loop budget."""
        self._stamps = []

    def backoff_for(self, attempt: int) -> float:
        """Seconds to wait after the ``attempt``-th (1-based) restart."""
        if not self.backoff:
            return 0.0
        return self.backoff[min(max(attempt, 1) - 1, len(self.backoff) - 1)]


@dataclasses.dataclass
class SupervisorState:
    """Supervisor-known facts handed to the observer on each poll.

    The telemetry layer enriches this with deployment identity, resource
    gauges, and the OUTSIDE manager verdict (process-table + status-file
    freshness) to build the tier-1 heartbeat.
    """

    health: dict | None            # last /health body (None => unreachable)
    child_alive: bool              # the supervised `bobi agent start` child
    supervisor_uptime_s: float
    restart_count: int             # windowed, from RestartBudget
    last_restart_reason: str | None
    last_restart_at: float | None
    # Authoritative liveness bookkeeping the telemetry layer reads instead of
    # keeping a parallel (and easy-to-desync) copy. ``ever_healthy`` is
    # PER-BOOT: it resets on every respawn, so a post-restart boot window is not
    # mistaken for a wedge. ``health_fail_count`` is the consecutive-miss count
    # the supervisor already debounces its restart on, so telemetry can apply
    # the same confirm before reporting a wedge.
    ever_healthy: bool
    health_fail_count: int
    # Load-grace context (#903): set while a verdict was deferred this
    # poll, None otherwise. The heartbeat renders it as an additive block so a
    # consumer can tell "stalled but legitimately busy" from "stalled".
    load_grace: dict | None = None
    # Operator intent (#9): "running" normally, "stopped" after an operator
    # `stop`. Lets telemetry report an intentionally-stopped manager as
    # `stopped` rather than a probe FAILURE (intentional != broken).
    desired_state: str = "running"


class SupervisorObserver:
    """No-op observer. Telemetry subclasses (or duck-types) this.

    Kept a plain class with no-op methods so the ported unit tests construct a
    ``Supervisor`` without one and the pure state machine is exercised in
    isolation.
    """

    def poll(self, state: SupervisorState) -> None:  # pragma: no cover - no-op
        pass

    def lifecycle(self, event: str, **fields) -> None:  # pragma: no cover
        pass


_NULL_OBSERVER = SupervisorObserver()


class CompositeObserver(SupervisorObserver):
    """Fan one observer slot out to several, each isolated fail-open.

    Lets the telemetry publisher and the incident alerter (#4) both watch the
    supervisor without either's failure reaching the other - or the restart
    state machine.
    """

    def __init__(self, observers):
        self._observers = list(observers)

    def poll(self, state: SupervisorState) -> None:
        for obs in self._observers:
            try:
                obs.poll(state)
            except Exception:
                # warning, not debug: a persistently-broken observer (e.g. the
                # alerter that exists to prevent silent failures) must itself
                # fail visibly, just never fatally.
                log.warning("supervisor: observer poll failed (%r)", obs,
                            exc_info=True)

    def lifecycle(self, event: str, **fields) -> None:
        for obs in self._observers:
            try:
                obs.lifecycle(event, **fields)
            except Exception:
                log.warning("supervisor: observer lifecycle failed (%r, %s)",
                            obs, event, exc_info=True)


class Supervisor:
    """Spawn-manage the manager child and self-heal a wedged director.

    Pure process management + health polling; the wedge *decision* lives in the
    module-level :func:`is_wedged`. Time, sleep, the health probe, the child
    spawn and the escalation are injectable so the state machine is fully
    unit-testable without real processes or wall-clock waits - the acceptance
    test drives a real child (a stub manager) end to end.
    """

    def __init__(self, start_args, config: SupervisorConfig, *,
                 project_root: Path | None = None,
                 now_fn=time.time, sleep_fn=time.sleep,
                 spawn_fn=None, health_fn=None, announce_fn=None,
                 load_fn=None,
                 observer: SupervisorObserver | None = None):
        self.start_args = list(start_args)
        self.config = config
        self.project_root = project_root
        self._now = now_fn
        self._sleep = sleep_fn
        self._spawn_fn = spawn_fn or self._default_spawn
        self._health_fn = health_fn or self._default_health
        self._load_fn = load_fn or self._default_load_fn
        self._announce_fn = announce_fn
        self._observer = observer or _NULL_OBSERVER
        self._budget = RestartBudget(config.max_restarts, config.restart_window,
                                     config.backoff)
        self._proc = None
        self._stop = False
        self._stall_count = 0
        self._fail_count = 0
        self._child_started_at = 0.0
        self._child_healthy_since: float | None = None
        # Load-grace state (#903). ``_load_prev`` is the per-poll CPU
        # baseline; ``_load_grace`` is the block reported to the observer while
        # a verdict is deferred; ``_grace_spell_start`` marks the start of the
        # current unresponsive spell the cap bounds; ``_load_evidence`` caches
        # the poll-start sample so a same-poll verdict uses the full-interval
        # delta rather than re-reading /proc moments later.
        # Opaque sampler-owned baseline. The production load sampler uses a
        # CpuSample; injected test/platform samplers may use another shape.
        self._load_prev: object | None = None
        self._load_grace: dict | None = None
        self._grace_spell_start: float | None = None
        self._load_evidence: dict | None = None
        # Last observed child liveness, maintained by _cycle so the observer
        # report never has to re-poll the child (a second poll would consume a
        # test double's poll sequence and, in production, is simply redundant).
        self._child_alive = False
        self._started_at = 0.0
        # Telemetry bookkeeping (additive, never affects a restart decision).
        self._last_health: dict | None = None
        self._last_restart_reason: str | None = None
        self._last_restart_at: float | None = None
        # Operator control plane (#9): admin commands arrive on the AdminListener
        # thread and append onto this queue under the lock; the main loop is the
        # SOLE executor (process management must stay single-threaded). A FIFO
        # queue (not one flag per verb) so two commands landing in the same poll
        # both apply, in order - `stop` then `start` must end running, not drop
        # the loser. `_desired_state` (running|stopped) lets an operator `stop`
        # hold the manager down without the crash path relaunching it.
        self._lock = threading.Lock()
        self._pending_ops: collections.deque[str] = collections.deque()
        self._desired_state = "running"

    # --- defaults (real process / real HTTP) -----------------------------

    def _default_spawn(self):
        root = (self.project_root or paths.bobi_root()).resolve()
        cmd = [
            sys.executable, "-m", "bobi.cli",
            "agent", paths.agent_name_for_root(root), "start",
            *self.start_args,
        ]
        log.info("supervisor: spawning manager child: %s", " ".join(cmd))
        return subprocess.Popen(cmd)

    def _port_file(self) -> Path:
        return paths.state_path(self.project_root) / "manager-health.port"

    def _default_health(self):
        port_file = self._port_file()
        try:
            port = int(port_file.read_text().strip())
        except (OSError, ValueError):
            return None
        from bobi import manager_health
        return manager_health.health(f"http://127.0.0.1:{port}")

    def _default_load_fn(self, manager_pid, previous):
        from .load import load_evidence
        return load_evidence(manager_pid, previous,
                             pegged_ratio=self.config.load_pegged_ratio,
                             tree_cpu_ratio=self.config.load_tree_cpu_ratio)

    # --- child lifecycle --------------------------------------------------

    def _respawn(self) -> None:
        self._proc = self._spawn_fn()
        self._child_started_at = self._now()
        self._child_healthy_since = None
        self._child_alive = True
        self._stall_count = 0
        self._fail_count = 0
        # Fresh process tree, fresh baseline: stale descendant samples (and a
        # pid reused by an unrelated process) must never feed the gate.
        self._load_prev = None
        self._load_grace = None
        self._grace_spell_start = None
        self._load_evidence = None

    def _kill_child(self) -> None:
        # Known, bounded limitation: a *wedged* manager may be too stuck to run
        # its own SIGTERM cleanup before term_grace elapses and we SIGKILL it.
        # Its detached agent grandchildren (spawned start_new_session=True, in
        # their own process groups) are then reparented to the container's PID-1
        # init, which reaps them. Blast radius is bounded by the restart budget:
        # at most ~max_restarts orphaned trees per window before the supervisor
        # exits non-zero and the orchestrator restarts the whole machine (a
        # clean container). Cause-agnostic whole-manager restart is the spec's
        # deliberate trade; full grandchild sweeping is deferred.
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=self.config.term_grace)
                except subprocess.TimeoutExpired:
                    log.warning("supervisor: child did not exit on SIGTERM, "
                                "sending SIGKILL")
                    proc.kill()
                    proc.wait(timeout=self.config.term_grace)
        except Exception:
            log.exception("supervisor: error terminating manager child")

    def _terminate_child(self) -> None:
        """Graceful shutdown path (supervisor received SIGTERM/SIGINT)."""
        self._kill_child()

    # --- escalation -------------------------------------------------------

    def _escalate(self, reason: str, now: float) -> None:
        # The budget is shared across wedge and crash restarts, so report the
        # actual count rather than attributing the whole window to one cause.
        restarts = self._budget.count(now)
        msg = (f"manager self-heal supervisor exhausted its restart budget "
               f"({restarts} restart(s) in the last "
               f"{int(self.config.restart_window)}s, limit "
               f"{self.config.max_restarts}); latest trigger was a {reason}. "
               f"Exiting non-zero so the machine restarts. The build is likely "
               f"broken and needs a human - restarting into the same wall will "
               f"not fix it.")
        log.error("supervisor: %s", msg)
        # Fleet-visible escalation: budget exhaustion is a terminal state that
        # must not be silent (no silent terminal states, epic #1). The Slack
        # announce below stays as the human channel; the lifecycle event makes
        # it observable to the control plane as well.
        self._observer.lifecycle("budget_exhausted", reason=reason,
                                  restart_count=restarts)
        self._announce(msg)

    def _announce(self, message: str) -> None:
        """Best-effort human escalation on budget exhaustion.

        Routine single restarts are stdout/log only; only the budget-exhaustion
        escalation is announced. Env-gated and fail-open: if no channel/token is
        configured the escalation degrades to the (already loud) error log
        rather than blocking the non-zero exit.
        """
        if self._announce_fn is not None:
            try:
                self._announce_fn(message)
            except Exception:
                log.exception("supervisor: escalation announce hook failed")
            return
        token = (os.environ.get("BOBI_SLACK_BOT_TOKEN")
                 or os.environ.get("SLACK_BOT_TOKEN"))
        channel = os.environ.get("WATCHDOG_ALERT_CHANNEL")
        if not (token and channel):
            log.warning("supervisor: WATCHDOG_ALERT_CHANNEL / Slack token not "
                        "set - budget-exhaustion escalation is log-only")
            return
        try:
            from bobi.slack import post_slack_message
            post_slack_message(token, channel, message)
        except Exception:
            log.exception("supervisor: failed to post escalation to Slack")

    def _note_restart(self, reason: str, now: float, **fields) -> None:
        """Record + announce a restart edge (telemetry only). Extra ``fields``
        ride along on the lifecycle event for the alerter (#4): ``charged``
        says whether THIS restart drew on the loop budget (the alerter's
        loop-membership signal - ``restart_count`` alone is just the window
        count and can be stale), plus the crash path's exit_code /
        healthy_uptime_s for the payload."""
        self._last_restart_reason = reason
        self._last_restart_at = now
        self._observer.lifecycle("manager_restarted", reason=reason,
                                  restart_count=self._budget.count(now),
                                  **fields)

    # --- operator control plane (#9) --------------------------------------
    #
    # Thread-safe request setters, called from the AdminListener thread. They
    # only RECORD intent; :meth:`_apply_operator_actions` (main loop) executes
    # it, so process management stays single-threaded.

    def request_manager_restart(self) -> None:
        with self._lock:
            self._pending_ops.append("restart")

    def request_manager_stop(self) -> None:
        with self._lock:
            self._pending_ops.append("stop")

    def request_manager_start(self) -> None:
        with self._lock:
            self._pending_ops.append("start")

    def desired_state(self) -> str:
        with self._lock:
            return self._desired_state

    def _apply_operator_actions(self) -> None:
        """Drain and execute every pending operator request in FIFO order (main
        loop). Applying all queued ops in order is what makes `stop` then `start`
        (or a restart mid-stream) resolve to the operator's last intent instead
        of silently dropping a command that was already acknowledged."""
        with self._lock:
            ops = list(self._pending_ops)
            self._pending_ops.clear()
        for op in ops:
            if op == "restart":
                self._operator_restart_manager()
            elif op == "stop":
                self._operator_stop_manager()
            elif op == "start":
                self._operator_start_manager()

    def _operator_restart_manager(self) -> None:
        now = self._now()
        log.warning("supervisor: operator-requested manager restart")
        self._kill_child()
        # Intentional restart: clear the crash/wedge budget so an operator
        # bounce neither counts toward nor is blocked by it.
        self._budget.reset()
        with self._lock:
            self._desired_state = "running"
        self._respawn()
        self._note_restart("operator", now)

    def _operator_stop_manager(self) -> None:
        with self._lock:
            already = self._desired_state == "stopped"
            self._desired_state = "stopped"
        if already:
            return
        log.warning("supervisor: operator-requested manager stop")
        self._kill_child()
        self._child_alive = False
        self._last_health = None
        self._load_grace = None
        self._grace_spell_start = None
        self._load_evidence = None
        self._observer.lifecycle("manager_stopped", reason="operator")

    def _operator_start_manager(self) -> None:
        with self._lock:
            self._desired_state = "running"
        if self._proc is not None and self._proc.poll() is None:
            return  # already running (idempotent)
        log.warning("supervisor: operator-requested manager start")
        self._respawn()
        self._observer.lifecycle("manager_started", reason="operator")

    # --- restart decisions ------------------------------------------------

    def _refresh_load_baseline(self) -> None:
        """Refresh the per-poll CPU baseline for the load gate (#903).

        The busy-descendant check diffs cpu ticks across two samples, so the
        FIRST verdict of a heavy period already needs a baseline from the
        previous poll. Cheap (one /proc walk per poll interval) and fail-open:
        a read failure just leaves the baseline stale for this poll. The
        evidence is cached for the verdict path so a same-poll verdict uses
        this full-interval delta instead of re-reading /proc moments later (a
        within-poll delta would read as ~0 busy and the gate would never open).
        """
        proc = self._proc
        if proc is None or not self.config.load_grace_enabled:
            self._load_evidence = None
            return
        try:
            evidence = self._load_fn(proc.pid, self._load_prev)
        except Exception:
            log.warning("supervisor: load baseline refresh failed - clearing "
                        "any active grace", exc_info=True)
            self._load_evidence = None
            self._load_grace = None
            self._grace_spell_start = None
            return
        self._load_evidence = evidence
        self._load_prev = evidence.get("sample")
        # Evidence is authoritative on every poll, not only on polls where a
        # liveness verdict reaches its confirmation point. Clearing here keeps
        # separate busy stretches from accumulating against one spell cap and
        # makes unreadable/inactive evidence fail closed immediately.
        if not evidence.get("active"):
            self._load_grace = None
            self._grace_spell_start = None

    def _defer_for_load(self, deferred: str) -> bool:
        """True when an ambiguous liveness verdict must be DEFERRED (#903).

        Sanctioned heavy work saturates the host while remaining productive;
        under that pressure probe misses, stalled turns and even a
        load-induced ``status=error`` read as failure to the restart machine
        while nothing is actually broken (issue #903 restarted a healthy
        instance off exactly this). When the host is pegged AND the manager's
        own descendant tree is the thing consuming CPU, the verdict is
        ambiguous - defer it: no budget charge, no restart.

        The exemption is bounded three ways, all derived from live state:

        - evidence is re-sampled every poll, so the gate reopens the moment
          the busy processes finish or die - nothing persisted can outlive
          them, and a parallel heavy command cannot overwrite or release a
          sibling's state (there is no per-holder state at all);
        - a *spell* - one continuous unresponsive stretch - longer than
          ``config.load_grace_max`` drops the deferral so a genuinely dead
          manager still escalates; any working poll (a live, non-stalled
          health response) resets the spell;
        - a real child exit never reaches this gate (the crash path in
          :meth:`_handle_child_exit` runs first and stays authoritative), and
          unreadable evidence fails CLOSED on the exemption: uncertainty never
          defers a restart.

        ``deferred`` names the verdict being deferred (``probe_miss`` /
        ``stalled_turn`` / ``dead_director``) and rides along in the additive
        heartbeat block for diagnosis.
        """
        if not self.config.load_grace_enabled:
            return False
        proc = self._proc
        if proc is None:
            return False
        now = self._now()
        evidence = self._load_evidence
        if evidence is None:
            # The poll-start sample failed or was skipped - try one fresh read
            # so a transient /proc hiccup cannot silently disable the gate.
            try:
                evidence = self._load_fn(proc.pid, self._load_prev)
            except Exception:
                log.warning("supervisor: load evidence read failed - not "
                            "deferring", exc_info=True)
                self._load_grace = None
                self._grace_spell_start = None
                return False
            self._load_prev = evidence.get("sample")
        if not evidence.get("active"):
            self._load_grace = None
            self._grace_spell_start = None
            return False
        if self._grace_spell_start is None:
            self._grace_spell_start = now
        spell = now - self._grace_spell_start
        if (self.config.load_grace_max > 0
                and spell > self.config.load_grace_max):
            log.error("supervisor: load grace exceeded %.0fs of continuous "
                      "unresponsiveness (load1=%s, %s busy descendant(s)) - "
                      "the %s verdict is no longer deferred",
                      spell, evidence.get("load1"),
                      evidence.get("busy_descendants"), deferred)
            self._load_grace = None
            self._grace_spell_start = None
            return False
        self._load_grace = {
            "active": True,
            "since": self._grace_spell_start,
            "spell_s": round(spell, 1),
            "deferred": deferred,
            "load1": evidence.get("load1"),
            "ncpu": evidence.get("ncpu"),
            "busy_descendants": evidence.get("busy_descendants", 0),
            "tree_cpu_cores": evidence.get("tree_cpu_cores"),
            "tree_cpu_ratio": evidence.get("tree_cpu_ratio"),
        }
        return True

    def _restart_wedge(self, reason: str = "wedge") -> int | None:
        """Restart a confirmed-wedged or positively-dead manager; escalate if
        out of budget. ``reason`` ("wedge" | "dead") keeps the log line, the
        restart telemetry and the budget-exhaustion escalation truthful about
        which trigger fired; the restart/budget/backoff machine is identical
        for both."""
        now = self._now()
        dead = reason == "dead"
        if self._budget.exhausted(now):
            self._escalate("dead-director loop" if dead else "wedge loop", now)
            return EXIT_BUDGET_EXHAUSTED
        attempt = self._budget.count(now) + 1
        if dead:
            log.warning("supervisor: director dead (status=error) - restarting "
                        "manager immediately (attempt %d/%d in window)",
                        attempt, self.config.max_restarts)
        else:
            log.warning("supervisor: director wedged (idle past %.0fs in an "
                        "active turn) - restarting manager (attempt %d/%d in "
                        "window)", self.config.stall_threshold, attempt,
                        self.config.max_restarts)
        self._kill_child()
        self._budget.record(now)
        self._note_restart(reason, now, charged=True)
        self._respawn()
        self._interruptible_sleep(self._budget.backoff_for(attempt))
        return None

    def _handle_child_exit(self, returncode) -> int | None:
        """Crash-relaunch path with fast-crash containment.

        A child that exits before it has been healthy for ``min_healthy_uptime``
        is a *fast crash* - it counts to the shared budget and obeys backoff, so
        a boot-crashing build cannot tight-loop. A child that ran healthy and
        then crashed once is a transient: relaunched without charging the loop
        budget.
        """
        now = self._now()
        healthy_for = (0.0 if self._child_healthy_since is None
                       else now - self._child_healthy_since)
        fast_crash = healthy_for < self.config.min_healthy_uptime
        log.error("supervisor: manager child exited (rc=%s) after %.0fs healthy "
                  "uptime (fast_crash=%s)", returncode, healthy_for, fast_crash)
        if fast_crash:
            if self._budget.exhausted(now):
                self._escalate("crash loop", now)
                return EXIT_BUDGET_EXHAUSTED
            attempt = self._budget.count(now) + 1
            self._budget.record(now)
            self._respawn()
            self._note_restart("crash", now, charged=True,
                               exit_code=returncode,
                               healthy_uptime_s=round(healthy_for, 1))
            log.warning("supervisor: relaunched after fast crash "
                        "(attempt %d/%d in window)", attempt,
                        self.config.max_restarts)
            self._interruptible_sleep(self._budget.backoff_for(attempt))
        else:
            log.warning("supervisor: relaunching after a transient crash "
                        "(ran healthy %.0fs - not charged to the loop budget)",
                        healthy_for)
            self._respawn()
            self._note_restart("crash", now, charged=False,
                               exit_code=returncode,
                               healthy_uptime_s=round(healthy_for, 1))
        return None

    # --- one poll cycle ---------------------------------------------------

    def _cycle(self) -> int | None:
        """Evaluate the child once. Returns an exit code to stop, else None."""
        # 1. Did the child exit on its own? (crash-relaunch path)
        assert self._proc is not None, "_cycle called with no child process"
        rc = self._proc.poll()
        if rc is not None:
            self._child_alive = False
            self._last_health = None
            return self._handle_child_exit(rc)
        self._child_alive = True

        # Per-poll load baseline: one /proc walk so a verdict that fires THIS
        # poll already has a full-interval CPU delta to judge against.
        self._refresh_load_baseline()

        # 2. Probe health.
        payload = self._health_fn()
        self._last_health = payload
        if payload is None:
            # During boot the child is alive but has not written its port file
            # yet - that is not a wedge, so we wait it out (fail-open: a manager
            # that has never been healthy is never restarted for an unreachable
            # probe). Once the child HAS been healthy, a parse/connection
            # failure is a wedge signal after N consecutive misses with the
            # child still alive (covers the daemon-thread health server dying
            # under an otherwise-live process).
            if self._child_healthy_since is None:
                log.debug("supervisor: health not up yet (child still booting)")
                return None
            self._fail_count += 1
            log.warning("supervisor: health probe failed (%d/%d consecutive)",
                        self._fail_count, self.config.confirm_polls)
            if self._fail_count >= self.config.confirm_polls:
                if self._defer_for_load("probe_miss"):
                    log.warning("supervisor: load grace - deferring the "
                                "probe-miss verdict")
                    self._fail_count = 0
                    return None
                return self._restart_wedge()
            return None
        self._fail_count = 0

        # 3. Apply the wedge discriminator to the manager block.
        manager = payload.get("manager") or {}
        status = manager.get("status")
        idle = manager.get("idle_seconds")

        # A dead director restarts immediately - no idle test, no confirm_polls
        # debounce (#12). `error` is the one status no other recovery path can
        # see (see DEAD_STATES); the shared budget still bounds repeated
        # error-deaths and escalates on exhaustion exactly like the wedge path.
        # Under load grace the verdict defers (#903): a starved health
        # thread can surface as `error` while the director is merely busy.
        if status in DEAD_STATES:
            if self._defer_for_load("dead_director"):
                log.warning("supervisor: load grace - deferring the dead-"
                            "director verdict")
                return None
            return self._restart_wedge("dead")

        # Credit healthy uptime only once the DIRECTOR is actually addressable
        # (status running/idle), not merely because the health server answered.
        # The health server is a daemon thread that survives a dead director and
        # the missing-entry guard reports status="starting", so crediting on any
        # non-None payload would misclassify a boot-looping build (director
        # crashes, health still answers) as a "transient" crash and relaunch it
        # unbounded. Gating here keeps fast-crash containment honest.
        if self._child_healthy_since is None and status in ("running", "idle"):
            self._child_healthy_since = self._now()
        if is_wedged(status, idle, self.config.stall_threshold):
            self._stall_count += 1
            assert idle is not None  # guaranteed by is_wedged
            log.warning("supervisor: director stalled (status=%s, idle=%.0fs) "
                        "- confirm %d/%d", status, float(idle),
                        self._stall_count, self.config.confirm_polls)
            if self._stall_count >= self.config.confirm_polls:
                if self._defer_for_load("stalled_turn"):
                    log.warning("supervisor: load grace - deferring the "
                                "stalled-turn verdict")
                    self._stall_count = 0
                    return None
                return self._restart_wedge()
        else:
            self._stall_count = 0
            # A working poll - a live, non-stalled health response - ends the
            # unresponsive spell: the cap resets, and any active grace block
            # clears so the heartbeat stops claiming a deferral that no longer
            # applies.
            self._load_grace = None
            self._grace_spell_start = None
        return None

    # --- observer report --------------------------------------------------

    def _report(self) -> SupervisorState:
        now = self._now()
        return SupervisorState(
            health=self._last_health,
            child_alive=self._child_alive,
            supervisor_uptime_s=max(0.0, now - self._started_at),
            restart_count=self._budget.count(now),
            last_restart_reason=self._last_restart_reason,
            last_restart_at=self._last_restart_at,
            # Per-boot: _child_healthy_since resets to None on every _respawn.
            ever_healthy=self._child_healthy_since is not None,
            health_fail_count=self._fail_count,
            desired_state=self._desired_state,
            load_grace=self._load_grace,
        )

    # --- top-level loop ---------------------------------------------------

    def request_stop(self) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        def _on_term(signum, frame):
            log.info("supervisor: received signal %s - forwarding to child and "
                     "shutting down", signum)
            self._stop = True
            # Forward immediately so the manager begins its graceful shutdown
            # now rather than after the current poll/backoff sleep returns
            # (Python resumes an interrupted time.sleep instead of raising).
            proc = self._proc
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.send_signal(signum)
                except Exception:
                    log.debug("supervisor: could not forward signal to child",
                              exc_info=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _on_term)
            except (ValueError, OSError):
                pass  # not on the main thread (e.g. driven from a test)

    def _interruptible_sleep(self, duration: float) -> None:
        """Sleep up to ``duration``, waking early once a stop is requested.

        Plain ``time.sleep`` resumes after a signal rather than raising, so a
        long poll/backoff sleep would otherwise delay container shutdown by up
        to that duration. Slicing lets the loop notice ``_stop`` promptly. The
        injected ``_sleep`` is still used for each slice so unit tests keep full
        control of time.
        """
        if duration <= 0:
            self._sleep(0)
            return
        remaining = duration
        step = 0.5
        while remaining > 0 and not self._stop:
            self._sleep(min(step, remaining))
            remaining -= step

    def run(self) -> int:
        """Spawn the manager and supervise it until stopped or budget-exhausted.

        Returns the process exit code: 0 on graceful stop, non-zero
        (:data:`EXIT_BUDGET_EXHAUSTED`) when the restart budget is exhausted so
        the orchestrator's machine restart policy escalates.
        """
        self._install_signal_handlers()
        self._started_at = self._now()
        self._respawn()
        self._observer.lifecycle("manager_started")
        try:
            while not self._stop:
                # Execute any operator command first (it may restart/stop/start
                # the child), then supervise - UNLESS the operator has stopped
                # the manager, in which case there is no child to supervise and
                # the crash path must not relaunch it. Heartbeats keep flowing.
                self._apply_operator_actions()
                code = self._cycle() if self.desired_state() == "running" else None
                # Report every poll (including the cycles that restarted) so the
                # heartbeat reflects the freshest verdict; a restart edge already
                # emitted its own lifecycle event above.
                try:
                    self._observer.poll(self._report())
                except Exception:
                    log.debug("supervisor: observer poll failed", exc_info=True)
                if code is not None:
                    return code
                if self._stop:
                    break
                self._interruptible_sleep(self.config.poll_interval)
        finally:
            self._terminate_child()
            self._observer.lifecycle("manager_stopped")
        return 0
