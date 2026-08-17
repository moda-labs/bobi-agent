"""Deduped Slack incident alerting for the supervisor (issue #4).

Design: the issue #4 comment (2026-07-14). A second :class:`SupervisorObserver`
composed alongside :class:`~.telemetry.Telemetry` via ``CompositeObserver``: it
consumes the SAME lifecycle edges the supervisor already emits and turns them
into deduped human alerts, so the ported restart state machine is untouched and
an alerting failure can never affect a restart decision (every handler is
fail-open).

The incident model:

- An incident OPENS at crash-loop onset - the 2nd budget-charged restart
  (reason crash/wedge/dead) inside the restart window. One SOFT Slack notice
  per incident. From outside the child a preflight failure IS a boot
  crash-loop, so this is also the "alert on a never-self-healing preflight"
  path, minutes after boot instead of never.
- Budget exhaustion posts the LOUD alert. The supervisor cannot observe its
  own Fly-level park (it is dead by then), so the incident file counts
  consecutive exhaustion cycles across machine restarts and the alert says how
  close the instance is to parking (``machine_restart_cap`` mirrors the
  fly.toml ``[[restart]]`` bound from #3); the at-cap alert is marked terminal.
- The incident CLOSES once a manager stays healthy past ``min_healthy_uptime``:
  one RECOVERED notice, the cycle counter resets, and a matching lifecycle
  event mirrors the ``probe_failing``/``probe_recovered`` edge semantics.

Incident state persists on the volume (the run root's state dir) because
machine restarts recycle the supervisor process - in-memory dedup would reset
exactly when dedup matters.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json

from .config import SupervisorConfig
from .supervision import SupervisorObserver, SupervisorState, is_wedged

log = logging.getLogger(__name__)

# The 2nd budget-charged restart inside the window opens the incident: one
# fast crash can be a transient, two in a window is a loop forming. The
# ``charged`` event field is the loop-membership signal (an operator restart
# or a transient healthy-uptime crash is never charged); ``restart_count``
# alone is just the window count and can be stale after a recovery.
_SOFT_AT = 2

STATE_FILE = "supervisor-incident.json"

# Alert posts run on the supervision loop (bounded to a handful per incident),
# so they get a short timeout: a hanging Slack must not stall wedge/crash
# detection for the library's default 10s.
_POST_TIMEOUT = 3.0


def _default_post(message: str) -> bool:
    """Post to the configured alert channel; True when delivered.

    Same env gates as ``Supervisor._announce``: ``WATCHDOG_ALERT_CHANNEL`` plus
    a Slack token. Unset degrades to log-only (the pre-#4 fleet state), never
    an error.
    """
    token = (os.environ.get("BOBI_SLACK_BOT_TOKEN")
             or os.environ.get("SLACK_BOT_TOKEN"))
    channel = os.environ.get("WATCHDOG_ALERT_CHANNEL")
    if not (token and channel):
        log.warning("supervisor: WATCHDOG_ALERT_CHANNEL / Slack token not set "
                    "- incident alert is log-only: %s", message)
        return False
    from bobi.slack import post_slack_message
    post_slack_message(token, channel, message, timeout=_POST_TIMEOUT)
    return True


class SlackAlerter(SupervisorObserver):
    """Turn supervisor lifecycle edges into deduped Slack incident alerts."""

    def __init__(self, *, project_root: Path | None, config: SupervisorConfig,
                 identity: dict | None = None, now_fn=time.time,
                 post_fn=None, lifecycle_fn=None, state_path=None):
        self.project_root = project_root
        self.config = config
        self.identity = identity or {}
        self._now = now_fn
        self._post_fn = post_fn or _default_post
        # Optional bus emit (wired to Telemetry.lifecycle) so incident edges
        # also land in the tier-2 trail; absent in bare unit tests.
        self._lifecycle_fn = lifecycle_fn
        if state_path is None:
            from bobi import paths
            state_path = paths.state_path(project_root) / STATE_FILE
        self._state_path = Path(state_path)
        self._incident: dict | None = self._load()
        # Recovery tracking (per-boot, in-memory: a machine restart that stays
        # healthy will simply re-derive it before closing the incident).
        self._healthy_since: float | None = None
        # Whether THIS process posted the rich exhaustion alert - the plain
        # ported announce only fires as a fallback (see announce_fallback).
        self._exhaustion_posted = False

    # --- incident persistence (all fail-open) ---------------------------

    def _load(self) -> dict | None:
        try:
            data = json.loads(self._state_path.read_text())
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _save(self) -> None:
        try:
            atomic_write_json(self._state_path, self._incident or {}, indent=None)
        except Exception:
            log.debug("supervisor: could not persist incident state",
                      exc_info=True)

    def _clear(self) -> None:
        self._incident = None
        try:
            self._state_path.unlink(missing_ok=True)
        except Exception:
            log.debug("supervisor: could not clear incident state",
                      exc_info=True)

    # --- observer interface ----------------------------------------------

    def lifecycle(self, event: str, **fields) -> None:
        try:
            if event == "manager_restarted":
                self._on_restart(fields)
            elif event == "budget_exhausted":
                self._on_exhausted(fields)
        except Exception:
            log.debug("supervisor: alerter lifecycle handling failed (%s)",
                      event, exc_info=True)

    def poll(self, state: SupervisorState) -> None:
        try:
            self._check_recovery(state)
        except Exception:
            log.debug("supervisor: alerter recovery check failed",
                      exc_info=True)

    def announce_fallback(self, message: str) -> None:
        """``announce_fn`` for the Supervisor. ``_escalate`` emits
        ``budget_exhausted`` (handled above: rich payload, cycle counter,
        deduped) BEFORE it announces, so the plain ported message goes out only
        when the rich alert could not be posted."""
        if self._exhaustion_posted:
            return
        try:
            self._post_fn(message)
        except Exception:
            log.exception("supervisor: escalation announce failed")

    # --- triggers ----------------------------------------------------------

    def _on_restart(self, fields: dict) -> None:
        reason = fields.get("reason")
        count = fields.get("restart_count") or 0
        # Only a budget-CHARGED restart can open an incident. The count alone
        # is not enough: a transient healthy-uptime crash reports the stale
        # window count of earlier restarts, which after a recovery would
        # reopen a fresh incident for one isolated crash.
        if not fields.get("charged") or count < _SOFT_AT:
            return
        now = self._now()
        if self._incident is None:
            self._incident = {"opened_at": now, "soft_alerted": False,
                              "exhaust_cycles": 0}
        if self._incident.get("soft_alerted"):
            return  # dedup: at most one soft notice per incident
        # Marked alerted even on a log-only post: the dedup promise is "at
        # most one", not "retry until Slack works".
        self._post(self._soft_message(reason, count, fields))
        self._incident["soft_alerted"] = True
        self._save()
        self._emit("crash_looping", reason=reason, restart_count=count)

    def _on_exhausted(self, fields: dict) -> None:
        now = self._now()
        if self._incident is None:
            self._incident = {"opened_at": now, "soft_alerted": True,
                              "exhaust_cycles": 0}
        cycles = int(self._incident.get("exhaust_cycles") or 0) + 1
        self._incident["exhaust_cycles"] = cycles
        if self._post(self._exhausted_message(fields, cycles)):
            self._exhaustion_posted = True
        self._save()

    def _currently_healthy(self, state: SupervisorState) -> bool:
        """The manager is healthy RIGHT NOW, not merely 'was healthy this
        boot': ``ever_healthy`` is a per-boot latch that stays True while a
        director wedges (idle climbing) or its health thread dies, and closing
        an incident on the latch would post a false RECOVERED and reset the
        park counter mid-incident. Re-derive the live verdict instead."""
        if state.desired_state == "stopped" or not state.ever_healthy:
            return False
        if state.health_fail_count:
            return False
        mgr = (state.health or {}).get("manager") or {}
        status = mgr.get("status")
        if status not in ("running", "idle"):
            return False
        return not is_wedged(status, mgr.get("idle_seconds"),
                             self.config.stall_threshold)

    def _check_recovery(self, state: SupervisorState) -> None:
        if not self._currently_healthy(state):
            self._healthy_since = None
            return
        now = self._now()
        if self._healthy_since is None:
            self._healthy_since = now
        if self._incident is None:
            return
        if now - self._healthy_since < self.config.min_healthy_uptime:
            return
        opened = self._incident.get("opened_at") or now
        cycles = int(self._incident.get("exhaust_cycles") or 0)
        duration = round(max(0.0, now - float(opened)), 1)
        self._clear()  # closing resets the machine-cycle counter too
        self._post(self._recovered_message(duration, cycles))
        self._emit("incident_recovered", downtime_s=duration,
                   machine_cycles=cycles or None)

    # --- helpers -----------------------------------------------------------

    def _post(self, message: str) -> bool:
        try:
            return bool(self._post_fn(message))
        except Exception:
            log.exception("supervisor: incident alert post failed")
            return False

    def _emit(self, event: str, **fields) -> None:
        if self._lifecycle_fn is None:
            return
        try:
            self._lifecycle_fn(event, **fields)
        except Exception:
            log.debug("supervisor: alerter lifecycle emit failed (%s)", event,
                      exc_info=True)

    def _who(self) -> str:
        return (f"{self.identity.get('fleet', '?')}/"
                f"{self.identity.get('instance', '?')}")

    def _app_name(self) -> str:
        return (os.environ.get("FLY_APP_NAME")
                or f"{self.identity.get('fleet', '?')}-"
                   f"{self.identity.get('instance', '?')}")

    def _logs_pointer(self) -> str:
        return f"logs: fly logs -a {self._app_name()}"

    def _soft_message(self, reason: str, count: int, fields: dict) -> str:
        extras = []
        if fields.get("exit_code") is not None:
            extras.append(f"last exit_code={fields['exit_code']}")
        if fields.get("healthy_uptime_s") is not None:
            extras.append(f"healthy_uptime={fields['healthy_uptime_s']}s")
        detail = (" (" + ", ".join(extras) + ")") if extras else ""
        return (f"[bobi] {self._who()} is crash-looping: {count} {reason} "
                f"restart(s) in the last {int(self.config.restart_window)}s "
                f"(budget {self.config.max_restarts}){detail}. Escalates to a "
                f"machine restart at budget exhaustion.\n{self._logs_pointer()}")

    def _exhausted_message(self, fields: dict, cycles: int) -> str:
        cap = self.config.machine_restart_cap
        reason = fields.get("reason") or "restart loop"
        count = fields.get("restart_count")
        head = (f"[bobi] {self._who()} restart budget exhausted "
                f"(machine cycle {cycles}/{cap}): {count} restart(s) in "
                f"{int(self.config.restart_window)}s, latest trigger was a "
                f"{reason}.")
        if cycles >= cap:
            # Stated as an inference, not a fact: the supervisor is dead
            # before the orchestrator decides, and Fly resets its own retry
            # counter on any healthy start while this counter only resets on
            # a SUSTAINED recovery - the two can drift on a flapping box.
            tail = (f" {cycles} consecutive exhaustion cycles without a "
                    "healthy recovery: the machine has likely hit the "
                    "orchestrator's restart cap and PARKED (stopped, staying "
                    "stopped). Human intervention required - verify with "
                    f"fly status -a {self._app_name()}.")
        else:
            tail = (" The supervisor exits non-zero so the orchestrator "
                    f"restarts the machine; the instance parks after ~{cap} "
                    "consecutive cycles. The build is likely broken and needs "
                    "a human - restarting into the same wall will not fix it.")
        return head + tail + "\n" + self._logs_pointer()

    def _recovered_message(self, duration: float, cycles: int) -> str:
        via = f" across {cycles} machine cycle(s)" if cycles else ""
        return (f"[bobi] {self._who()} recovered: manager healthy for "
                f"{int(self.config.min_healthy_uptime)}s after an incident "
                f"lasting {duration}s{via}.")
