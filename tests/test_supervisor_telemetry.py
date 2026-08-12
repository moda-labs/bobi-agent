"""Unit tests for the sidecar telemetry layer (issue #5).

Covers the platform-adapter identity resolution, the OUTSIDE manager verdict
(process-table + status-file freshness fused with the health body), and the
Telemetry observer: heartbeat shape + schema, the probe_failing/probe_recovered
episode edges, and lifecycle passthrough. Publishing is injected so no network
or bubble is needed.
"""

import pytest

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.identity import resolve_deployment_identity
from bobi.supervisor.probe import derive_manager_status
from bobi.supervisor.supervision import SupervisorState
from bobi.supervisor.telemetry import Telemetry


def _cfg(**kw):
    base = dict(stall_threshold=600, status_file_stale=600)
    base.update(kw)
    return SupervisorConfig(**base)


class Clock:
    def __init__(self, start=1000.0, step=1.0):
        self.t = start
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


# --- platform adapter / identity -----------------------------------------

class TestIdentity:

    def test_canonical_key_from_bobi_env(self, monkeypatch):
        monkeypatch.setenv("BOBI_FLEET", "acme")
        monkeypatch.setenv("BOBI_INSTANCE", "acme-prod-1")
        ident = resolve_deployment_identity()
        assert ident["fleet"] == "acme"
        assert ident["instance"] == "acme-prod-1"

    def test_fly_enrichment(self, monkeypatch):
        monkeypatch.setenv("BOBI_INSTANCE", "i1")
        monkeypatch.setenv("FLY_APP_NAME", "app")
        monkeypatch.setenv("FLY_MACHINE_ID", "148e",)
        monkeypatch.setenv("FLY_REGION", "iad")
        ident = resolve_deployment_identity()
        assert ident["platform"] == "fly"
        assert ident["machine"] == "148e"
        assert ident["region"] == "iad"

    def test_k8s_enrichment_via_downward_api(self, monkeypatch):
        monkeypatch.setenv("BOBI_INSTANCE", "i1")
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("POD_NAME", "bobi-abc")
        monkeypatch.setenv("NODE_NAME", "node-7")
        ident = resolve_deployment_identity()
        assert ident["platform"] == "k8s"
        assert ident["machine"] == "bobi-abc"
        assert ident["node"] == "node-7"

    def test_explicit_overrides_win_on_any_platform(self, monkeypatch):
        monkeypatch.setenv("BOBI_INSTANCE", "i1")
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("POD_NAME", "bobi-abc")
        monkeypatch.setenv("BOBI_MACHINE_ID", "override-m")
        monkeypatch.setenv("BOBI_REGION", "override-r")
        monkeypatch.setenv("BOBI_NODE", "override-n")
        ident = resolve_deployment_identity()
        assert ident["machine"] == "override-m"
        assert ident["region"] == "override-r"
        assert ident["node"] == "override-n"

    def test_fleet_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOBI_FLEET", raising=False)
        monkeypatch.setenv("BOBI_INSTANCE", "solo")
        ident = resolve_deployment_identity()
        assert ident["fleet"] == "default"
        assert ident["instance"] == "solo"


# --- the OUTSIDE manager verdict -----------------------------------------

class TestDeriveManagerStatus:

    def test_running_and_idle_pass_through(self):
        for st in ("running", "idle", "starting"):
            out = derive_manager_status(
                {"manager": {"status": st, "idle_seconds": 1}},
                child_alive=True, manager_pid_alive=True, status_file_age=1,
                ever_healthy=True, config=_cfg())
            assert out == st

    def test_wedged_from_health_body(self):
        out = derive_manager_status(
            {"manager": {"status": "running", "idle_seconds": 9999}},
            child_alive=True, manager_pid_alive=True, status_file_age=1,
            ever_healthy=True, config=_cfg())
        assert out == "wedged"

    def test_wedged_from_status_file_corroboration(self):
        # Health body claims a fresh active turn, but the on-disk state.json has
        # not advanced past the freshness bound - the independent signal wins.
        out = derive_manager_status(
            {"manager": {"status": "running", "idle_seconds": 1}},
            child_alive=True, manager_pid_alive=True, status_file_age=9999,
            ever_healthy=True, config=_cfg())
        assert out == "wedged"

    def test_down_when_nothing_alive(self):
        out = derive_manager_status(
            None, child_alive=False, manager_pid_alive=False,
            status_file_age=None, ever_healthy=True, config=_cfg())
        assert out == "down"

    def test_starting_when_never_healthy_and_unreachable(self):
        out = derive_manager_status(
            None, child_alive=True, manager_pid_alive=True,
            status_file_age=None, ever_healthy=False, config=_cfg())
        assert out == "starting"

    def test_wedged_when_was_healthy_then_silent(self):
        # Health thread died under a live process - observable only from outside.
        out = derive_manager_status(
            None, child_alive=True, manager_pid_alive=True,
            status_file_age=None, ever_healthy=True, config=_cfg())
        assert out == "wedged"


# --- Telemetry observer ---------------------------------------------------

def _telemetry(published, **kw):
    def publish(topic, source, data, project_root):
        published.append((topic, source, data))
        return True
    return Telemetry(project_root=None, config=_cfg(),
                     identity={"fleet": "f", "instance": "i", "platform": "x",
                               "machine": None, "region": None, "node": None},
                     now_fn=Clock(), publish_fn=publish, **kw)


def _state(status="idle", idle=1, child_alive=True, health=..., **kw):
    if health is ...:
        health = {"pid": 4242, "manager": {"session": "mgr", "status": status,
                                           "idle_seconds": idle},
                  "sessions": [{"name": "mgr", "role": "manager",
                                "status": status}]}
    base = dict(health=health, child_alive=child_alive, supervisor_uptime_s=5.0,
                restart_count=0, last_restart_reason=None, last_restart_at=None,
                ever_healthy=True, health_fail_count=0)
    base.update(kw)
    return SupervisorState(**base)


class TestTelemetryHeartbeat:

    def test_poll_publishes_heartbeat_with_expected_shape(self, monkeypatch):
        # No pid file on disk -> manager_pid_alive False; force the derived
        # status off the health body only by stubbing the probe helpers.
        import bobi.supervisor.telemetry as tele
        monkeypatch.setattr(tele.probe, "manager_pid_alive", lambda root: True)
        monkeypatch.setattr(tele.probe, "status_file_age",
                            lambda root, session, now: 1.0)
        published = []
        t = _telemetry(published)
        t.poll(_state(status="idle"))

        assert len(published) == 1
        topic, source, data = published[0]
        assert topic == "fleet/heartbeat"
        assert source == "fleet"
        assert data["deployment"]["instance"] == "i"
        assert data["manager"]["status"] == "idle"
        assert data["manager"]["healthy"] is True
        assert data["manager"]["pid"] == 4242
        assert data["sessions"] == [{"name": "mgr", "role": "manager",
                                     "status": "idle"}]
        # Phase A schema invariants.
        assert data["event_client"] is None
        assert set(data["resources"]) == {"disk_free_mb", "mem_free_mb",
                                          "mem_pct"}
        assert set(data["expectations"]) == {"subscriptions", "monitors"}
        assert "generated_at" in data

    def test_wedge_emits_probe_failing_then_recovered(self, monkeypatch):
        import bobi.supervisor.telemetry as tele
        monkeypatch.setattr(tele.probe, "manager_pid_alive", lambda root: True)
        monkeypatch.setattr(tele.probe, "status_file_age",
                            lambda root, session, now: 1.0)
        published = []
        t = _telemetry(published)

        # Healthy first: heartbeat only, no episode.
        t.poll(_state(status="idle"))
        # Wedged: heartbeat + a single probe_failing edge.
        t.poll(_state(status="running", idle=9999))
        # Still wedged: heartbeat only, NO second probe_failing (one episode).
        t.poll(_state(status="running", idle=9999))
        # Recovered: heartbeat + probe_recovered.
        t.poll(_state(status="idle"))

        lifecycle = [d for (topic, s, d) in published
                     if topic == "fleet/lifecycle"]
        events = [d["event"] for d in lifecycle]
        assert events == ["probe_failing", "probe_recovered"]
        assert lifecycle[0]["status"] == "wedged"
        assert lifecycle[0]["deployment"]["instance"] == "i"
        # Four heartbeats regardless of episodes.
        assert sum(1 for (topic, s, d) in published
                   if topic == "fleet/heartbeat") == 4

    def test_expected_busy_keeps_ambiguous_failure_out_of_probe_episode(
            self, monkeypatch):
        import bobi.supervisor.telemetry as tele
        monkeypatch.setattr(tele.probe, "manager_pid_alive", lambda root: True)
        monkeypatch.setattr(tele.probe, "status_file_age",
                            lambda root, session, now: 9999.0)
        published = []
        t = _telemetry(published)

        busy = {"active": True, "lease_count": 2, "expires_at": 2000.0}
        t.poll(_state(status="running", idle=9999, expected_busy=busy))

        heartbeat = [d for (topic, _, d) in published
                     if topic == "fleet/heartbeat"][-1]
        assert heartbeat["manager"]["status"] == "running"
        assert heartbeat["manager"]["healthy"] is True
        assert heartbeat["manager"]["expected_busy"] == busy
        assert not [d for (topic, _, d) in published
                    if topic == "fleet/lifecycle"]

    def test_post_restart_boot_not_reported_wedged(self, monkeypatch):
        """A fresh boot window after a restart (ever_healthy False, health not up
        yet) must report 'starting', not 'wedged' - the manager is booting, not
        stuck. Regression for the _ever_healthy latch that never reset."""
        import bobi.supervisor.telemetry as tele
        monkeypatch.setattr(tele.probe, "manager_pid_alive", lambda root: True)
        monkeypatch.setattr(tele.probe, "status_file_age",
                            lambda root, session, now: None)
        monkeypatch.setattr(tele.probe, "read_manager_pid", lambda root: None)
        published = []
        t = _telemetry(published)
        # health None, ever_healthy False (just respawned), no prior healthy poll.
        t.poll(_state(health=None, ever_healthy=False, health_fail_count=0))

        hb = [d for (topic, s, d) in published if topic == "fleet/heartbeat"]
        assert hb[0]["manager"]["status"] == "starting"
        assert not [d for (topic, s, d) in published if topic == "fleet/lifecycle"]

    def test_transient_health_miss_is_debounced(self, monkeypatch):
        """A single unreachable-probe miss on a previously-healthy manager must
        NOT flip the fleet view to wedged; only a confirmed miss streak does.
        Regression for the no-debounce probe_failing flap."""
        import bobi.supervisor.telemetry as tele
        monkeypatch.setattr(tele.probe, "manager_pid_alive", lambda root: True)
        monkeypatch.setattr(tele.probe, "status_file_age",
                            lambda root, session, now: None)
        monkeypatch.setattr(tele.probe, "read_manager_pid", lambda root: None)
        published = []
        t = _telemetry(published)  # HEALTH_MISS_CONFIRM = 2

        t.poll(_state(status="idle"))                       # healthy baseline
        t.poll(_state(health=None, ever_healthy=True, health_fail_count=1))  # miss #1
        # No wedge yet: carried forward the last confirmed status.
        assert not [d for (tp, s, d) in published if tp == "fleet/lifecycle"]
        hb = [d for (tp, s, d) in published if tp == "fleet/heartbeat"]
        assert hb[-1]["manager"]["status"] == "idle"

        t.poll(_state(health=None, ever_healthy=True, health_fail_count=2))  # confirmed
        events = [d["event"] for (tp, s, d) in published if tp == "fleet/lifecycle"]
        assert events == ["probe_failing"]
        hb = [d for (tp, s, d) in published if tp == "fleet/heartbeat"]
        assert hb[-1]["manager"]["status"] == "wedged"

    def test_lifecycle_passthrough(self):
        published = []
        t = _telemetry(published)
        t.lifecycle("manager_restarted", reason="wedge", restart_count=2)
        t.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)

        lifecycle = [d for (topic, s, d) in published
                     if topic == "fleet/lifecycle"]
        assert [d["event"] for d in lifecycle] == ["manager_restarted",
                                                   "budget_exhausted"]
        assert lifecycle[0]["reason"] == "wedge"
        assert lifecycle[0]["restart_count"] == 2
        assert lifecycle[1]["event"] == "budget_exhausted"


class TestSupervisorLoggingIsStdoutOnly:
    """The sidecar is the terminal process; stdout is the only channel an
    orchestrator reads.

    Regression guard for the repo reorg (Lane 1). `bobi agent <name>
    supervise` reaches run() through the `agent` group, whose runtime binding
    calls _attach_runtime_log() and points the root logger at
    <state>/manager.log. run() must strip that: a supervisor logging into a
    file inside the container it supervises is invisible to `docker logs`,
    `kubectl logs`, and Fly.
    """

    def _run_with_file_handler_attached(self, tmp_path, monkeypatch):
        import logging
        from bobi.supervisor import __main__ as sm

        log_file = tmp_path / "manager.log"
        handler = logging.FileHandler(log_file)
        root = logging.getLogger()
        root.addHandler(handler)

        # Stop run() before it does any supervising - the logging setup is
        # what is under test, and it happens first.
        class _Stop(Exception):
            pass

        def _boom(*a, **k):
            raise _Stop()

        monkeypatch.setattr(sm.SupervisorConfig, "from_env", staticmethod(_boom))
        try:
            with pytest.raises(_Stop):
                sm.run(tmp_path, ["--foreground"])
        finally:
            root.removeHandler(handler)
            handler.close()
        return root

    def test_run_strips_an_inherited_file_handler(self, tmp_path, monkeypatch):
        import logging

        root = self._run_with_file_handler_attached(tmp_path, monkeypatch)
        assert not [h for h in root.handlers if isinstance(h, logging.FileHandler)], (
            "run() left a FileHandler attached - supervisor output would go to a "
            "file inside the container instead of stdout"
        )

    def test_run_logs_to_stdout(self, tmp_path, monkeypatch):
        import logging
        import sys

        root = self._run_with_file_handler_attached(tmp_path, monkeypatch)
        streams = [
            h.stream for h in root.handlers if isinstance(h, logging.StreamHandler)
        ]
        assert any(s is sys.stdout for s in streams), (
            f"run() did not attach a stdout handler (streams: {streams})"
        )
