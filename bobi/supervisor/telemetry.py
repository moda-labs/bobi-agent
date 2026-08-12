"""Tier-1 heartbeat + tier-2 lifecycle publishing (issue #5).

The telemetry layer is a :class:`SupervisorObserver`: the supervisor hands it a
report every poll (for the heartbeat) and calls it on the lifecycle edges it
*acts* on (started/stopped/restarted/budget_exhausted). The observed-condition
episodes (``probe_failing`` / ``probe_recovered``) are derived here from the
OUTSIDE verdict this layer computes for each heartbeat, so one poll produces
both the snapshot and any condition transition.

Observability-first: this is outbound-only. Heartbeats and lifecycle events are
bubble-signed HTTP publishes to ``fleet/heartbeat`` / ``fleet/lifecycle`` - no
WS subscription, no event-server trust changes. Every method is fail-open:
telemetry must never take the supervisor down.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import probe
from .bus import default_publish as _default_publish
from .identity import resolve_deployment_identity
from .config import SupervisorConfig
from .snapshot import SUPERVISOR_VERSION, build_heartbeat
from .supervision import SupervisorObserver, SupervisorState

log = logging.getLogger(__name__)

HEARTBEAT_TOPIC = "fleet/heartbeat"
LIFECYCLE_TOPIC = "fleet/lifecycle"

# The OUTSIDE verdicts that count as a failing episode for probe_failing.
_BAD_STATES = frozenset({"wedged", "down"})
_HEALTHY_STATES = frozenset({"running", "idle"})

# Consecutive unreachable-probe misses before a previously-healthy manager is
# REPORTED wedged. One miss can be a fluke (a dropped response under load); two
# in a row is a real unreachability. Deliberately a small fixed constant, NOT
# the supervisor's WATCHDOG_CONFIRM_POLLS: that value gates the *restart* and an
# operator may set it high, which must not suppress the wedge *report*.
HEALTH_MISS_CONFIRM = 2


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


class Telemetry(SupervisorObserver):
    """Publish tier-1 heartbeats + tier-2 lifecycle events onto the bus."""

    def __init__(self, *, project_root: Path | None,
                 config: SupervisorConfig,
                 identity: dict | None = None,
                 supervisor_version: str = SUPERVISOR_VERSION,
                 now_fn=time.time,
                 publish_fn=_default_publish,
                 ensure_bubble_fn=None):
        self.project_root = project_root
        self.config = config
        self.identity = identity or resolve_deployment_identity(project_root)
        self.supervisor_version = supervisor_version
        self._now = now_fn
        self._publish_fn = publish_fn
        self._ensure_bubble_fn = ensure_bubble_fn
        # Derived-verdict state carried across polls. ``_last_status`` is the
        # last CONFIRMED verdict, reported through an unconfirmed transient
        # health miss so a single dropped /health probe doesn't flap the fleet
        # view to wedged and back (the supervisor debounces its restart the same
        # way, via confirm_polls).
        self._last_status: str | None = None
        self._probe_failing = False
        self._probe_since: float | None = None
        # The most recent heartbeat snapshot, exposed for the admin `status`
        # command (#9) so a status reply reuses the tier-1 body verbatim.
        self.last_snapshot: dict | None = None

    # --- startup ----------------------------------------------------------

    def join_bubble(self, es_url: str | None = None) -> None:
        """JOIN the instance bubble so publishes are signed. Best-effort.

        The manager mints the bubble on first registration; the sidecar joins
        the same trust domain (ensure_bubble is lock-protected + idempotent and
        converges with the manager's concurrent mint). If it fails, publishes
        simply skip (they need the bubble key) until it exists - never fatal.
        """
        try:
            if self._ensure_bubble_fn is not None:
                self._ensure_bubble_fn()
                return
            from bobi.config import Config
            from bobi.events.server import ensure_bubble
            url = es_url or Config.load(self.project_root).event_server_url \
                or "http://localhost:8080"
            ensure_bubble(url, self.project_root)
        except Exception:
            log.warning("supervisor: could not join the instance bubble yet - "
                        "telemetry will publish once it exists",
                        exc_info=True)

    # --- observer interface ----------------------------------------------

    def poll(self, state: SupervisorState) -> None:
        try:
            self._publish_heartbeat(state)
        except Exception:
            log.debug("supervisor: heartbeat publish failed", exc_info=True)

    def lifecycle(self, event: str, **fields) -> None:
        # Supervisor-driven edge (started/stopped/restarted/budget_exhausted).
        try:
            self._emit_lifecycle(event, **fields)
        except Exception:
            log.debug("supervisor: lifecycle publish failed (%s)", event,
                      exc_info=True)

    # --- internals --------------------------------------------------------

    def _publish_heartbeat(self, state: SupervisorState) -> None:
        now = self._now()
        health = state.health
        session = probe.manager_session_name(health)
        mgr_pid_alive = probe.manager_pid_alive(self.project_root)
        sf_age = probe.status_file_age(self.project_root, session, now)

        # An operator-stopped manager is intentionally down: report it as
        # `stopped`, never a probe verdict, so it does not read as a failure
        # (and does not open a probe_failing episode below).
        if state.desired_state == "stopped":
            derived = "stopped"
            self._last_status = derived
        else:
            raw = probe.derive_manager_status(
                health,
                child_alive=state.child_alive,
                manager_pid_alive=mgr_pid_alive,
                status_file_age=sf_age,
                ever_healthy=state.ever_healthy,
                config=self.config,
            )
            if state.expected_busy and raw in ("wedged", "error"):
                mgr = (health or {}).get("manager") or {}
                reported = mgr.get("status")
                derived = reported if reported in ("starting", "running") \
                    else (self._last_status or "running")
                self._last_status = derived
            # Debounce the health-MISS -> wedged flip: a wedge derived purely
            # from an unreachable probe is only trustworthy after
            # HEALTH_MISS_CONFIRM consecutive misses; below that, carry the last
            # confirmed status forward rather than flapping the fleet view on a
            # single dropped probe. A wedge derived from the health BODY (a
            # stalled active turn) or a `down` verdict is a real observation and
            # is reported immediately.
            elif (raw == "wedged" and health is None
                    and state.health_fail_count < HEALTH_MISS_CONFIRM):
                derived = self._last_status or "starting"
            else:
                derived = raw
                self._last_status = derived
        healthy = derived in _HEALTHY_STATES

        manager_pid = (health or {}).get("pid") \
            or probe.read_manager_pid(self.project_root)

        snapshot = build_heartbeat(
            identity=self.identity, state=state, derived_status=derived,
            healthy=healthy, manager_pid=manager_pid, now=now,
            project_root=self.project_root,
            supervisor_version=self.supervisor_version,
        )
        self.last_snapshot = snapshot
        self._publish(HEARTBEAT_TOPIC, snapshot)

        if state.desired_state == "stopped":
            # An operator stop is not a recovery: close any open failing episode
            # silently rather than emitting a misleading probe_recovered into the
            # lifecycle trail (the manager was stopped, it did not heal).
            self._probe_failing = False
            self._probe_since = None
        else:
            # Edge-trigger the probe episode from the outside verdict.
            self._update_probe_episode(derived, now)

    def _update_probe_episode(self, derived: str, now: float) -> None:
        bad = derived in _BAD_STATES
        if bad and not self._probe_failing:
            self._probe_failing = True
            self._probe_since = now
            self._emit_lifecycle("probe_failing", status=derived,
                                 since=_iso(now))
        elif not bad and self._probe_failing:
            self._probe_failing = False
            downtime = None if self._probe_since is None else round(
                now - self._probe_since, 1)
            self._probe_since = None
            self._emit_lifecycle("probe_recovered", status=derived,
                                 downtime_s=downtime)

    def _emit_lifecycle(self, event: str, *, correlation_id: str | None = None,
                        **fields) -> None:
        payload = {
            "deployment": self.identity,
            "event": event,
            "generated_at": _iso(self._now()),
        }
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        payload.update({k: v for k, v in fields.items() if v is not None})
        self._publish(LIFECYCLE_TOPIC, payload)

    def _publish(self, topic: str, data: dict) -> None:
        try:
            self._publish_fn(topic, "fleet", data, self.project_root)
        except Exception:
            log.debug("supervisor: publish to %s failed", topic, exc_info=True)
