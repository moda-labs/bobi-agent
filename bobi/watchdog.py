"""Manager self-heal watchdog — a supervisor below the director (#464).

``stall-recovery`` is director→engineer: it recovers a stalled *engineer*
session from inside the director. By construction it cannot recover the
**director itself** — when the director wedges, nothing above it inside the
application can act. This module is that missing layer: ``bobi supervise``
spawns the manager as a child process, polls its health endpoint, and restarts
a wedged director with bounded retry, backoff, loud logging, and fail-open
safety.

It is **defense-in-depth**, not a replacement for #456/PR #460: #460 bounds the
one *known* wedge (the rotation reconnect, ``session.py`` mechanism #3); this
backstops *unknown* wedge classes by keying off **progress** (``last_activity``
+ turn state), not cause.

The core design problem is that ``last_activity`` freezes in two situations
that look identical from outside — a healthy director idling at ``inbox.recv``
and a director wedged mid-turn. The discriminator is the session status:

    Restart iff status ∈ {starting, running} AND idle_seconds > STALL_THRESHOLD.

An ``idle``/``stopped``/``done`` director is never restarted for inactivity, so
the watchdog never false-kills a quiet-but-healthy team.

See ``docs/specs/464-manager-self-heal-watchdog.md`` for the full design and the
open-decision resolutions (D1–D8).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from bobi import paths

log = logging.getLogger(__name__)

# The director is only restartable-for-inactivity while it is *making a turn*.
# These are the active states from session.py's status machine; idle/stopped/
# done/error are not active and are never treated as wedged.
ACTIVE_STATES = frozenset({"starting", "running"})

# Non-zero exit so Fly's machine restart policy takes over after the supervisor
# exhausts its in-container restart budget — escalation, not a silent park.
EXIT_BUDGET_EXHAUSTED = 70  # EX_SOFTWARE


def is_wedged(status, idle_seconds, stall_threshold: float) -> bool:
    """The wedge discriminator: a *positive* signal only.

    True iff the director is in an active turn state and has made zero registry
    progress for longer than ``stall_threshold``. Any uncertainty (unknown
    status, missing ``idle_seconds``) returns False — uncertainty must never
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
    # A non-finite idle (NaN, or Infinity — which json.loads accepts and which
    # would otherwise pass `> threshold`) is corrupt input, not a wedge signal:
    # fail open.
    if not math.isfinite(idle):
        return False
    return idle > stall_threshold


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("watchdog: ignoring non-numeric %s=%r, using %s",
                    name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("watchdog: ignoring non-numeric %s=%r, using %s",
                    name, raw, default)
        return default


def _env_backoff(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        vals = tuple(float(x) for x in raw.replace(",", " ").split())
        return vals or default
    except ValueError:
        log.warning("watchdog: ignoring malformed %s=%r, using %s",
                    name, raw, default)
        return default


@dataclasses.dataclass
class WatchdogConfig:
    """Tunables (D2: env vars with documented defaults; D3 values)."""

    poll_interval: float = 30.0       # WATCHDOG_POLL_INTERVAL
    stall_threshold: float = 600.0    # WATCHDOG_STALL_THRESHOLD (10 min)
    confirm_polls: int = 2            # WATCHDOG_CONFIRM_POLLS
    max_restarts: int = 3             # WATCHDOG_MAX_RESTARTS
    restart_window: float = 1800.0    # WATCHDOG_RESTART_WINDOW (30 min)
    backoff: tuple[float, ...] = (30.0, 60.0, 120.0)  # WATCHDOG_BACKOFF
    min_healthy_uptime: float = 120.0  # WATCHDOG_MIN_HEALTHY_UPTIME
    term_grace: float = 10.0          # SIGTERM→SIGKILL grace on the child

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            poll_interval=_env_float("WATCHDOG_POLL_INTERVAL", 30.0),
            stall_threshold=_env_float("WATCHDOG_STALL_THRESHOLD", 600.0),
            confirm_polls=_env_int("WATCHDOG_CONFIRM_POLLS", 2),
            max_restarts=_env_int("WATCHDOG_MAX_RESTARTS", 3),
            restart_window=_env_float("WATCHDOG_RESTART_WINDOW", 1800.0),
            backoff=_env_backoff("WATCHDOG_BACKOFF", (30.0, 60.0, 120.0)),
            min_healthy_uptime=_env_float("WATCHDOG_MIN_HEALTHY_UPTIME", 120.0),
            term_grace=_env_float("WATCHDOG_TERM_GRACE", 10.0),
        )


class RestartBudget:
    """Windowed restart counter shared by *both* restart paths (D8).

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

    def backoff_for(self, attempt: int) -> float:
        """Seconds to wait after the ``attempt``-th (1-based) restart."""
        if not self.backoff:
            return 0.0
        return self.backoff[min(max(attempt, 1) - 1, len(self.backoff) - 1)]


class Supervisor:
    """Spawn-manage the manager child and self-heal a wedged director.

    Pure process management + health polling; the wedge *decision* lives in the
    module-level :func:`is_wedged`. Time, sleep, the health probe, the child
    spawn and the escalation are injectable so the state machine is fully
    unit-testable without real processes or wall-clock waits — the acceptance
    test drives a real child (a stub manager) end to end.
    """

    def __init__(self, start_args, config: WatchdogConfig, *,
                 project_root: Path | None = None,
                 now_fn=time.time, sleep_fn=time.sleep,
                 spawn_fn=None, health_fn=None, announce_fn=None):
        self.start_args = list(start_args)
        self.config = config
        self.project_root = project_root
        self._now = now_fn
        self._sleep = sleep_fn
        self._spawn_fn = spawn_fn or self._default_spawn
        self._health_fn = health_fn or self._default_health
        self._announce_fn = announce_fn
        self._budget = RestartBudget(config.max_restarts, config.restart_window,
                                     config.backoff)
        self._proc = None
        self._stop = False
        self._stall_count = 0
        self._fail_count = 0
        self._child_started_at = 0.0
        self._child_healthy_since: float | None = None

    # --- defaults (real process / real HTTP) -----------------------------

    def _default_spawn(self):
        root = (self.project_root or paths.bobi_root()).resolve()
        cmd = [
            sys.executable, "-m", "bobi.cli",
            "agent", paths.agent_name_for_root(root), "start",
            *self.start_args,
        ]
        log.info("watchdog: spawning manager child: %s", " ".join(cmd))
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

    # --- child lifecycle --------------------------------------------------

    def _respawn(self) -> None:
        self._proc = self._spawn_fn()
        self._child_started_at = self._now()
        self._child_healthy_since = None
        self._stall_count = 0
        self._fail_count = 0

    def _kill_child(self) -> None:
        # Known, bounded limitation: a *wedged* manager may be too stuck to run
        # its own SIGTERM cleanup (cli.py) before term_grace elapses and we
        # SIGKILL it. Its detached agent grandchildren (spawned
        # start_new_session=True, in their own process groups) are then
        # reparented to Fly's PID-1 init, which reaps them. Blast radius is
        # bounded by the restart budget: at most ~max_restarts orphaned trees
        # per window before the supervisor exits non-zero and Fly restarts the
        # whole machine (a clean container). Cause-agnostic whole-manager
        # restart is the spec's deliberate trade (D4); full grandchild sweeping
        # is deferred.
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=self.config.term_grace)
                except subprocess.TimeoutExpired:
                    log.warning("watchdog: child did not exit on SIGTERM, "
                                "sending SIGKILL")
                    proc.kill()
                    proc.wait(timeout=self.config.term_grace)
        except Exception:
            log.exception("watchdog: error terminating manager child")

    def _terminate_child(self) -> None:
        """Graceful shutdown path (supervisor received SIGTERM/SIGINT)."""
        self._kill_child()

    # --- escalation (D7) --------------------------------------------------

    def _escalate(self, reason: str, now: float) -> None:
        # The budget is shared across wedge and crash restarts, so report the
        # actual count rather than attributing the whole window to one cause.
        restarts = self._budget.count(now)
        msg = (f"manager self-heal watchdog exhausted its restart budget "
               f"({restarts} restart(s) in the last "
               f"{int(self.config.restart_window)}s, limit "
               f"{self.config.max_restarts}); latest trigger was a {reason}. "
               f"Exiting non-zero so the machine restarts. The build is likely "
               f"broken and needs a human — restarting into the same wall will "
               f"not fix it.")
        log.error("watchdog: %s", msg)
        self._announce(msg)

    def _announce(self, message: str) -> None:
        """Best-effort human escalation on budget exhaustion (D7).

        Routine single restarts are stdout/Fly-log only; only the
        budget-exhaustion escalation is announced. Env-gated and fail-open: if
        no channel/token is configured the escalation degrades to the (already
        loud) error log rather than blocking the non-zero exit.
        """
        if self._announce_fn is not None:
            try:
                self._announce_fn(message)
            except Exception:
                log.exception("watchdog: escalation announce hook failed")
            return
        token = (os.environ.get("BOBI_SLACK_BOT_TOKEN")
                 or os.environ.get("SLACK_BOT_TOKEN"))
        channel = os.environ.get("WATCHDOG_ALERT_CHANNEL")
        if not (token and channel):
            log.warning("watchdog: WATCHDOG_ALERT_CHANNEL / Slack token not set "
                        "— budget-exhaustion escalation is log-only")
            return
        try:
            from bobi.slack import post_slack_message
            post_slack_message(token, channel, message)
        except Exception:
            log.exception("watchdog: failed to post escalation to Slack")

    # --- restart decisions ------------------------------------------------

    def _restart_wedge(self) -> int | None:
        """Restart a confirmed-wedged manager; escalate if out of budget."""
        now = self._now()
        if self._budget.exhausted(now):
            self._escalate("wedge loop", now)
            return EXIT_BUDGET_EXHAUSTED
        attempt = self._budget.count(now) + 1
        log.warning("watchdog: director wedged (idle past %.0fs in an active "
                    "turn) — restarting manager (attempt %d/%d in window)",
                    self.config.stall_threshold, attempt, self.config.max_restarts)
        self._kill_child()
        self._budget.record(now)
        self._respawn()
        self._interruptible_sleep(self._budget.backoff_for(attempt))
        return None

    def _handle_child_exit(self, returncode) -> int | None:
        """Crash-relaunch path with fast-crash containment (§6, D8).

        A child that exits before it has been healthy for ``min_healthy_uptime``
        is a *fast crash* — it counts to the shared budget and obeys backoff, so
        a boot-crashing build cannot tight-loop. A child that ran healthy and
        then crashed once is a transient: relaunched without charging the loop
        budget.
        """
        now = self._now()
        healthy_for = (0.0 if self._child_healthy_since is None
                       else now - self._child_healthy_since)
        fast_crash = healthy_for < self.config.min_healthy_uptime
        log.error("watchdog: manager child exited (rc=%s) after %.0fs healthy "
                  "uptime (fast_crash=%s)", returncode, healthy_for, fast_crash)
        if fast_crash:
            if self._budget.exhausted(now):
                self._escalate("crash loop", now)
                return EXIT_BUDGET_EXHAUSTED
            attempt = self._budget.count(now) + 1
            self._budget.record(now)
            self._respawn()
            log.warning("watchdog: relaunched after fast crash "
                        "(attempt %d/%d in window)", attempt,
                        self.config.max_restarts)
            self._interruptible_sleep(self._budget.backoff_for(attempt))
        else:
            log.warning("watchdog: relaunching after a transient crash "
                        "(ran healthy %.0fs — not charged to the loop budget)",
                        healthy_for)
            self._respawn()
        return None

    # --- one poll cycle ---------------------------------------------------

    def _cycle(self) -> int | None:
        """Evaluate the child once. Returns an exit code to stop, else None."""
        # 1. Did the child exit on its own? (crash-relaunch path)
        rc = self._proc.poll()
        if rc is not None:
            return self._handle_child_exit(rc)

        # 2. Probe health.
        payload = self._health_fn()
        if payload is None:
            # During boot the child is alive but has not written its port file
            # yet — that is not a wedge, so we wait it out (fail-open: a manager
            # that has never been healthy is never restarted for an unreachable
            # probe). Once the child HAS been healthy, a parse/connection
            # failure is a wedge signal after N consecutive misses with the
            # child still alive (covers the daemon-thread health server dying
            # under an otherwise-live process).
            if self._child_healthy_since is None:
                log.debug("watchdog: health not up yet (child still booting)")
                return None
            self._fail_count += 1
            log.warning("watchdog: health probe failed (%d/%d consecutive)",
                        self._fail_count, self.config.confirm_polls)
            if self._fail_count >= self.config.confirm_polls:
                return self._restart_wedge()
            return None
        self._fail_count = 0

        # 3. Apply the wedge discriminator to the manager block.
        manager = payload.get("manager") or {}
        status = manager.get("status")
        idle = manager.get("idle_seconds")

        # Credit healthy uptime only once the DIRECTOR is actually addressable
        # (status running/idle), not merely because the health server answered.
        # The health server is a daemon thread that survives a dead director and
        # the missing-entry guard reports status="starting", so crediting on any
        # non-None payload would misclassify a boot-looping build (director
        # crashes, health still answers) as a "transient" crash and relaunch it
        # unbounded. Gating here keeps fast-crash containment honest (§6).
        if self._child_healthy_since is None and status in ("running", "idle"):
            self._child_healthy_since = self._now()
        if is_wedged(status, idle, self.config.stall_threshold):
            self._stall_count += 1
            log.warning("watchdog: director stalled (status=%s, idle=%.0fs) "
                        "— confirm %d/%d", status, float(idle),
                        self._stall_count, self.config.confirm_polls)
            if self._stall_count >= self.config.confirm_polls:
                return self._restart_wedge()
        else:
            self._stall_count = 0
        return None

    # --- top-level loop ---------------------------------------------------

    def request_stop(self) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        def _on_term(signum, frame):
            log.info("watchdog: received signal %s — forwarding to child and "
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
                    log.debug("watchdog: could not forward signal to child",
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
        Fly's machine restart policy escalates.
        """
        self._install_signal_handlers()
        self._respawn()
        try:
            while not self._stop:
                code = self._cycle()
                if code is not None:
                    return code
                if self._stop:
                    break
                self._interruptible_sleep(self.config.poll_interval)
        finally:
            self._terminate_child()
        return 0
