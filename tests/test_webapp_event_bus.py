"""Unit tests for ``EventBusRuntime`` - the hosted webapp's ``TeamRuntime`` over
the fleet API.

The runtime is pure HTTP glue, so a fake ``/fleet`` server (an
``httpx.MockTransport``) exercises every method without a broker or supervisor:
dashboard/status shaping, the (fleet,instance) name codec, the command RPC
(issue + poll), and the typed-error mapping the public handlers rely on.
"""

import json

import httpx
import pytest

from bobi.webapp.runtime import TeamLifecycleError, UnknownTeam
from bobi.webapp.event_bus import EventBusRuntime, decode_name, encode_name

OPERATOR = "test-op-token"

# A CostSummary.to_dict() shape (bobi.costs) - what the supervisor's spend
# command folds from the local session state and returns under {"spend": ...}.
DEFAULT_SPEND = {
    "total_cost_usd": 1.25,
    "sessions_counted": 3,
    "by_provider": {"anthropic": 1.25},
    "by_model": {"claude-opus-4-8": 1.25},
    "by_session": {"bobi-x-manager": 1.25},
    "by_role": {"manager": 1.25},
    "estimated_cost_usd": 0.0,
    "estimated_by_model": {},
    "tokens_by_model": {},
}

# What the supervisor's session_log command folds from the local registry
# (#733 vertical 3): serialize_subagent rows + session_id/error/terminal_at,
# with pre-cap counts and the truncation flag.
DEFAULT_SESSION_LOG = {
    "sessions": [
        {"name": "bobi-x-manager", "role": "manager", "status": "idle",
         "is_manager": True, "error": "", "terminal_at": None,
         "ended": False, "session_id": "sid-mgr", "total_cost_usd": 0.5},
        {"name": "boom", "role": "engineer", "status": "failed",
         "is_manager": False, "error": "turn errored",
         "terminal_at": 1720000000.0, "ended": True,
         "session_id": "sid-boom", "total_cost_usd": 0.25},
    ],
    "counts": {"active": 1, "completed": 0, "failed": 1, "crashed": 0},
    "truncated": False,
}


class FakeFleet:
    """A minimal in-memory stand-in for the event-server ``/fleet`` API."""

    def __init__(self):
        self.instances: dict[tuple[str, str], dict] = {}
        self.commands: dict[str, dict] = {}
        self._n = 0
        # Per-command overrides: command name -> (status_code, view_status, payload)
        self.post_status: dict[str, int] = {}
        self.results: dict[str, dict] = {}
        # Per-instance spend override (else DEFAULT_SPEND), the set of
        # (fleet, instance) whose supervisor is wedged (every command 503s ->
        # TeamLifecycleError), and the set that is listed in /fleet/status but
        # 404s on a command POST (de-registered mid-fan-out -> UnknownTeam, a
        # sibling of TeamLifecycleError). Both are boxes the fan-out must skip.
        self.spend: dict[tuple[str, str], dict] = {}
        self.wedged: set[tuple[str, str]] = set()
        self.phantom: set[tuple[str, str]] = set()
        self.saw_auth: list[str | None] = []
        # Number of command-view GETs to answer with a transient 5xx before the
        # real view (simulates a Cloudflare edge/KV blip mid-poll).
        self.transient_5xx = 0

    def add_instance(self, fleet, instance, **over):
        detail = {
            "deployment": {"fleet": fleet, "instance": instance,
                           "platform": "fly", "region": "iad"},
            "manager": {"status": "idle", "pid": 4242},
            "reachability": "live",
            "versions": {"team_package": "1.2.3"},
            "sessions": [{"name": f"bobi-{instance}-manager",
                          "role": "manager", "status": "idle"}],
        }
        detail.update(over)
        self.instances[(fleet, instance)] = detail

    def _default_result(self, command):
        return {
            "restart": {"accepted": True, "action": "restart"},
            "stop": {"accepted": True, "action": "stop"},
            "start": {"accepted": True, "action": "start"},
            "roster": {"subagents": [{"name": "bobi-x-manager",
                                      "role": "manager", "is_manager": True,
                                      "total_cost_usd": 0.5}]},
            "transcript": {"messages": [{"role": "user", "text": "ping"},
                                        {"role": "agent", "text": "stub ack: ping"}]},
            "chat": {"delivered": True, "session": "bobi-x-manager"},
            "spend": {"spend": DEFAULT_SPEND},
            "session_log": DEFAULT_SESSION_LOG,
        }.get(command, {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.saw_auth.append(request.headers.get("authorization"))
        parts = request.url.path.strip("/").split("/")
        method = request.method

        if method == "GET" and request.url.path == "/fleet/status":
            return httpx.Response(200, json={
                "instances": [dict(v, deployment=v["deployment"])
                              for v in self.instances.values()]})

        # /fleet/instances/<f>/<i>[/commands[/<id>]]
        if parts[:2] == ["fleet", "instances"]:
            fleet, instance = parts[2], parts[3]
            if len(parts) == 4 and method == "GET":
                d = self.instances.get((fleet, instance))
                if not d:
                    return httpx.Response(404, json={"error": "unknown instance"})
                return httpx.Response(200, json=d)

            if len(parts) == 5 and parts[4] == "commands" and method == "POST":
                # A phantom is still in /fleet/status but 404s here (it
                # de-registered between the status read and this POST).
                if ((fleet, instance) not in self.instances
                        or (fleet, instance) in self.phantom):
                    return httpx.Response(404, json={"error": "unknown instance"})
                # A wedged supervisor cannot take any command (503, like a box
                # with no live supervisor) - the fan-out must skip it, not fail.
                if (fleet, instance) in self.wedged:
                    return httpx.Response(503, json={"error": "no live supervisor"})
                body = json.loads(request.content or b"{}")
                command = body.get("command")
                code = self.post_status.get(command, 202)
                if code != 202:
                    return httpx.Response(code, json={"error": "not deliverable"})
                self._n += 1
                cid = f"cmd-{self._n}"
                if command == "spend":
                    result = {"spend": self.spend.get((fleet, instance),
                                                       DEFAULT_SPEND)}
                else:
                    result = self.results.get(command,
                                              self._default_result(command))
                self.commands[cid] = {
                    "command_id": cid, "command": command,
                    "args": body.get("args"),
                    "status": "pending",
                    "result": result,
                }
                return httpx.Response(202, json={"command_id": cid,
                                                 "status": "pending"})

            if len(parts) == 6 and parts[4] == "commands" and method == "GET":
                if self.transient_5xx > 0:
                    self.transient_5xx -= 1
                    return httpx.Response(502, json={"error": "bad gateway"})
                cid = parts[5]
                view = self.commands.get(cid)
                if not view:
                    return httpx.Response(404, json={"error": "unknown command"})
                # First poll flips pending -> its terminal state, so the poll
                # loop is exercised at least once.
                out = dict(view)
                if view["status"] == "pending":
                    out["status"] = self.results.get(f"{view['command']}::status",
                                                     "done")
                    if out["status"] == "error":
                        out["error"] = "boom"
                    view["status"] = out["status"]
                return httpx.Response(200, json=out)

        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fleet():
    return FakeFleet()


@pytest.fixture
def runtime(fleet):
    client = httpx.Client(transport=httpx.MockTransport(fleet.handler),
                          base_url="http://fleet.test")
    return EventBusRuntime(fleet_api_url="http://fleet.test",
                           operator_token=OPERATOR, client=client,
                           command_timeout=3.0, poll_interval=0.01)


# --- name codec -----------------------------------------------------------

def test_name_codec_roundtrips():
    assert encode_name("acme", "prod-1") == "acme~prod-1"
    assert decode_name("acme~prod-1") == ("acme", "prod-1")


def test_decode_rejects_a_nameless_or_partial_id():
    for bad in ("noseparator", "~inst", "fleet~"):
        with pytest.raises(UnknownTeam):
            decode_name(bad)


# --- read model -----------------------------------------------------------

def test_dashboard_lists_cards_and_sends_operator_auth(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    fleet.add_instance("acme", "prod-2", reachability="unreachable",
                       manager={"status": "stopped", "pid": 0})
    dash = runtime.dashboard()
    assert dash["home"] == "fleet"
    cards = {c["name"]: c for c in dash["agents"]}
    assert cards["acme~prod-1"]["running"] is True
    assert cards["acme~prod-1"]["reachability"] == "live"
    assert "team 1.2.3" in cards["acme~prod-1"]["description"]
    # An unreachable, stopped instance is surfaced but not "running".
    assert cards["acme~prod-2"]["running"] is False
    assert cards["acme~prod-2"]["reachability"] == "unreachable"
    assert cards["acme~prod-2"]["manager_status"] == "stopped"
    assert fleet.saw_auth[0] == f"Bearer {OPERATOR}"


def test_team_status_unknown_instance_raises_unknown_team(runtime):
    with pytest.raises(UnknownTeam):
        runtime.team_status("acme~ghost")


# --- lifecycle ------------------------------------------------------------

def test_restart_stop_start_issue_commands(fleet, runtime):
    # A lifecycle command only ACKs; the pid surfaces on the next heartbeat, so
    # the immediate return reports pid 0 (unknown-yet), never a stale live pid.
    fleet.add_instance("acme", "prod-1")
    assert runtime.restart_team("acme~prod-1") == {"ok": True, "pid": 0}
    assert runtime.start_team("acme~prod-1") == {"ok": True, "pid": 0}
    stop = runtime.stop_team("acme~prod-1")
    assert stop == {"ok": True, "stopped": True, "pid": 0, "still_running": False}


def test_lifecycle_no_supervisor_maps_to_lifecycle_error(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    fleet.post_status["restart"] = 503
    with pytest.raises(TeamLifecycleError):
        runtime.restart_team("acme~prod-1")


def test_command_poll_survives_a_transient_5xx(fleet, runtime):
    # A momentary edge/KV blip mid-poll must not abort an in-flight command;
    # the poll loop keeps going to the deadline.
    fleet.add_instance("acme", "prod-1")
    fleet.transient_5xx = 2
    assert runtime.restart_team("acme~prod-1") == {"ok": True, "pid": 0}


# --- roster + transcript --------------------------------------------------

def test_subagents_uses_the_roster_command(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    roster = runtime.subagents("acme~prod-1")
    assert roster and roster[0]["is_manager"] is True
    assert roster[0]["total_cost_usd"] == 0.5


def test_subagents_unreachable_supervisor_raises(fleet, runtime):
    # One path (like LocalRuntime): an unreachable supervisor surfaces the
    # failure, not a silently-degraded stale roster.
    fleet.add_instance("acme", "prod-1")
    fleet.post_status["roster"] = 503  # roster command not deliverable
    with pytest.raises(TeamLifecycleError):
        runtime.subagents("acme~prod-1")


def test_messages_reads_the_transcript_command(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    msgs = runtime.messages("acme~prod-1", "bobi-prod-1-manager")
    assert msgs == [{"role": "user", "text": "ping"},
                    {"role": "agent", "text": "stub ack: ping"}]


# --- chat (submit-then-poll over the command RPC) -------------------------

def test_chat_submit_returns_command_id_without_polling(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    mid = runtime.chat_submit("acme~prod-1", "bobi-prod-1-manager", "ping")
    assert mid.startswith("cmd-")
    # Not yet polled: the command is still pending server-side.
    assert fleet.commands[mid]["status"] == "pending"


def test_chat_job_reports_pending_then_done(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    mid = runtime.chat_submit("acme~prod-1", "", "ping")
    # The fake flips pending->done on the first poll.
    assert runtime.chat_job("acme~prod-1", mid) == {"status": "done"}


def test_chat_job_reports_error(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    fleet.results["chat::status"] = "error"
    mid = runtime.chat_submit("acme~prod-1", "", "ping")
    job = runtime.chat_job("acme~prod-1", mid)
    assert job["status"] == "error" and job["error"] == "boom"


def test_chat_job_unknown_id_is_none(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    assert runtime.chat_job("acme~prod-1", "cmd-does-not-exist") is None


# --- session log (observability, #733 vertical 3) ---------------------------

def test_session_log_uses_the_command_rpc(fleet, runtime):
    # Box-consulting, like spend: the history lives only on the box, so a
    # command POST goes out (vs health's pure read-model GET).
    fleet.add_instance("acme", "prod-1")
    log = runtime.session_log("acme~prod-1")
    assert log == DEFAULT_SESSION_LOG
    assert any(c["command"] == "session_log" for c in fleet.commands.values())


def test_session_log_unreachable_supervisor_raises(fleet, runtime):
    # One path (like subagents/spend_summary): an unreachable supervisor
    # surfaces the failure rather than masking it with an empty history.
    fleet.add_instance("acme", "prod-1")
    fleet.post_status["session_log"] = 503
    with pytest.raises(TeamLifecycleError):
        runtime.session_log("acme~prod-1")


def test_session_log_unknown_instance_raises(runtime):
    with pytest.raises(UnknownTeam):
        runtime.session_log("acme~ghost")


def test_session_log_missing_payload_defaults(fleet, runtime):
    # A box that answers with a bare/odd payload must not 500 the panel.
    fleet.add_instance("acme", "prod-1")
    fleet.results["session_log"] = {}
    assert runtime.session_log("acme~prod-1") == {
        "sessions": [], "counts": {}, "truncated": False}


# --- system health (observability, #733) -----------------------------------

def test_health_summary_shapes_the_instance_detail(fleet, runtime):
    # Health is a pure read-model read: the detail already folds reachability,
    # the sidecar's manager summary, sessions, and the lifecycle trail. No
    # command-RPC leg - the box itself is never consulted.
    fleet.add_instance(
        "acme", "prod-1",
        manager={"status": "idle", "pid": 4242, "healthy": True,
                 "restart_count": 2, "last_restart_reason": "probe_failing",
                 "last_restart_at": 1720000000.0, "idle_seconds": 12.5},
        last_heartbeat_at="2026-07-13T10:00:00+00:00",
        lifecycle=[
            {"deployment": {"fleet": "acme", "instance": "prod-1"},
             "event": "probe_recovered", "status": "idle",
             "generated_at": "2026-07-13T09:59:00+00:00",
             "received_at": 1720000140000},
            {"deployment": {"fleet": "acme", "instance": "prod-1"},
             "event": "manager_restarted", "reason": "probe_failing",
             "generated_at": "2026-07-13T09:58:00+00:00",
             "received_at": 1720000080000},
        ],
    )
    out = runtime.health_summary("acme~prod-1")
    assert out["reachability"] == "live"
    assert out["last_heartbeat_at"] == "2026-07-13T10:00:00+00:00"
    mgr = out["manager"]
    assert mgr == {"status": "idle", "pid": 4242, "running": True,
                   "healthy": True, "restart_count": 2,
                   "last_restart_reason": "probe_failing",
                   "last_restart_at": 1720000000.0, "idle_seconds": 12.5}
    assert out["sessions"] == [{"name": "bobi-prod-1-manager",
                                "role": "manager", "status": "idle"}]
    # Trail order preserved (newest first from the read model); the redundant
    # per-entry deployment identity is stripped; box clock (`at`) and server
    # receipt (`received_at`) both carried.
    assert [e["event"] for e in out["lifecycle"]] == ["probe_recovered",
                                                      "manager_restarted"]
    assert out["lifecycle"][0] == {"event": "probe_recovered",
                                   "at": "2026-07-13T09:59:00+00:00",
                                   "received_at": 1720000140000,
                                   "status": "idle", "reason": None}
    assert out["lifecycle"][1]["reason"] == "probe_failing"
    assert all("deployment" not in e for e in out["lifecycle"])
    # No command was issued for any of it.
    assert fleet.commands == {}


def test_health_summary_unreachable_instance_still_reports(fleet, runtime):
    # An unreachable box cannot answer a command, but health must still render
    # - its silence IS the signal. The stale manager verdict rides along with
    # reachability so the frontend can show it as no-longer-trusted.
    fleet.add_instance("acme", "prod-2", reachability="unreachable",
                       manager={"status": "idle", "pid": 4242})
    out = runtime.health_summary("acme~prod-2")
    assert out["reachability"] == "unreachable"
    assert out["manager"]["status"] == "idle"
    assert out["manager"]["running"] is False


def test_down_manager_is_not_running_on_card_or_health(fleet, runtime):
    # The probe's "down" verdict (child exited AND manager pid gone,
    # bobi/supervisor/probe.py in the public repo) means nothing is running, even while the sidecar's
    # heartbeat keeps the instance "live". One shared derivation covers the
    # dashboard card and the health view.
    fleet.add_instance("acme", "prod-1",
                       manager={"status": "down", "pid": 0})
    assert runtime.team_status("acme~prod-1")["running"] is False
    out = runtime.health_summary("acme~prod-1")
    assert out["manager"]["running"] is False
    assert out["manager"]["status"] == "down"


def test_malformed_heartbeat_pid_reads_zero_not_500(fleet, runtime):
    fleet.add_instance("acme", "prod-1",
                       manager={"status": "idle", "pid": "n/a"})
    assert runtime.team_status("acme~prod-1")["pid"] == 0
    assert runtime.health_summary("acme~prod-1")["manager"]["pid"] == 0


def test_health_summary_tolerates_a_missing_manager_block(fleet, runtime):
    fleet.add_instance("acme", "prod-3", manager=None, sessions=None,
                       lifecycle=None)
    out = runtime.health_summary("acme~prod-3")
    assert out["manager"]["status"] == "unknown"
    assert out["manager"]["pid"] == 0
    assert out["manager"]["running"] is False
    assert out["sessions"] == []
    assert out["lifecycle"] == []


def test_health_summary_caps_the_lifecycle_trail(fleet, runtime):
    from bobi.webapp.event_bus import MAX_HEALTH_LIFECYCLE_EVENTS
    trail = [{"event": "restarted", "received_at": 1720000000000 - i}
             for i in range(MAX_HEALTH_LIFECYCLE_EVENTS + 25)]
    fleet.add_instance("acme", "prod-1", lifecycle=trail)
    out = runtime.health_summary("acme~prod-1")
    # Newest-first input, so the cap keeps the recent end.
    assert len(out["lifecycle"]) == MAX_HEALTH_LIFECYCLE_EVENTS
    assert out["lifecycle"][0]["received_at"] == 1720000000000


def test_health_summary_unknown_instance_raises_unknown_team(runtime):
    with pytest.raises(UnknownTeam):
        runtime.health_summary("acme~ghost")


# --- spend (observability, #733) ------------------------------------------

def test_spend_summary_returns_the_teams_cost_summary(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    assert runtime.spend_summary("acme~prod-1") == DEFAULT_SPEND


def test_spend_summary_unreachable_supervisor_raises(fleet, runtime):
    # One path (like subagents): an unreachable supervisor surfaces the failure,
    # not a silent {} that would read as "this team costs nothing".
    fleet.add_instance("acme", "prod-1")
    fleet.post_status["spend"] = 503
    with pytest.raises(TeamLifecycleError):
        runtime.spend_summary("acme~prod-1")


def test_fleet_spend_sums_and_ranks_per_team(fleet, runtime):
    fleet.add_instance("acme", "prod-1")
    fleet.add_instance("acme", "prod-2")
    fleet.spend[("acme", "prod-1")] = dict(DEFAULT_SPEND, total_cost_usd=2.0,
                                           sessions_counted=4)
    fleet.spend[("acme", "prod-2")] = dict(DEFAULT_SPEND, total_cost_usd=5.5,
                                           sessions_counted=1)
    out = runtime.fleet_spend()
    assert out["total_cost_usd"] == 7.5
    assert out["estimated_cost_usd"] == 0.0
    assert out["sessions_counted"] == 5
    # Highest-spend team first, each carrying its own total.
    assert [t["name"] for t in out["teams"]] == ["acme~prod-2", "acme~prod-1"]
    assert out["teams"][0] == {"name": "acme~prod-2", "total_cost_usd": 5.5,
                               "estimated_cost_usd": 0.0,
                               "sessions_counted": 1}


def test_fleet_spend_folds_estimated_separately(fleet, runtime):
    # A codex-brained team reports no recorded dollars, only a fold-time
    # estimate (#760). The estimate must ride its own field top-level and
    # per-tile, never mixed into recorded dollars, and rank the tile by
    # recorded + estimated (the LocalRuntime.fleet_spend sort).
    fleet.add_instance("acme", "prod-1")
    fleet.add_instance("acme", "codex-1")
    fleet.spend[("acme", "prod-1")] = dict(DEFAULT_SPEND, total_cost_usd=2.0,
                                           sessions_counted=4)
    fleet.spend[("acme", "codex-1")] = dict(
        DEFAULT_SPEND, total_cost_usd=0.0, estimated_cost_usd=3.75,
        sessions_counted=2)
    out = runtime.fleet_spend()
    assert out["total_cost_usd"] == 2.0
    assert out["estimated_cost_usd"] == 3.75
    assert [t["name"] for t in out["teams"]] == ["acme~codex-1", "acme~prod-1"]
    assert out["teams"][0] == {"name": "acme~codex-1", "total_cost_usd": 0.0,
                               "estimated_cost_usd": 3.75,
                               "sessions_counted": 2}


def test_fleet_spend_tolerates_a_pre_estimate_box(fleet, runtime):
    # Mid-rollout: a box still on a pre-0.44 bobi reports a spend dict with
    # no estimated_cost_usd key at all. It folds as 0, not a KeyError.
    fleet.add_instance("acme", "prod-1")
    legacy = {k: v for k, v in DEFAULT_SPEND.items()
              if not k.startswith(("estimated_", "tokens_"))}
    fleet.spend[("acme", "prod-1")] = dict(legacy, total_cost_usd=1.5)
    out = runtime.fleet_spend()
    assert out["total_cost_usd"] == 1.5
    assert out["estimated_cost_usd"] == 0.0
    assert out["teams"][0]["estimated_cost_usd"] == 0.0


def test_fleet_spend_tolerates_a_wedged_instance(fleet, runtime):
    # One dead box must not fail the dashboard: it still lists, contributing 0.
    fleet.add_instance("acme", "prod-1")
    fleet.add_instance("acme", "prod-2")
    fleet.spend[("acme", "prod-1")] = dict(DEFAULT_SPEND, total_cost_usd=3.0,
                                           sessions_counted=2)
    fleet.wedged.add(("acme", "prod-2"))
    out = runtime.fleet_spend()
    assert out["total_cost_usd"] == 3.0
    assert out["sessions_counted"] == 2
    teams = {t["name"]: t for t in out["teams"]}
    assert teams["acme~prod-2"] == {"name": "acme~prod-2", "total_cost_usd": 0.0,
                                    "estimated_cost_usd": 0.0,
                                    "sessions_counted": 0}


def test_fleet_spend_tolerates_a_deregistered_instance(fleet, runtime):
    # TOCTOU: an instance listed in /fleet/status can 404 on its spend POST
    # (UnknownTeam - a SIBLING of TeamLifecycleError, not a subclass). The
    # fan-out must still skip it, not 500 the whole dashboard.
    fleet.add_instance("acme", "prod-1")
    fleet.add_instance("acme", "prod-2")
    fleet.spend[("acme", "prod-1")] = dict(DEFAULT_SPEND, total_cost_usd=4.0,
                                           sessions_counted=1)
    fleet.phantom.add(("acme", "prod-2"))
    out = runtime.fleet_spend()
    assert out["total_cost_usd"] == 4.0
    teams = {t["name"]: t for t in out["teams"]}
    assert teams["acme~prod-2"]["total_cost_usd"] == 0.0


def test_fleet_spend_caches_the_fan_out_within_the_ttl(fleet):
    # The ~4s frontend poll must not re-fan-out every request; a folded result
    # is served for the TTL, then re-fans.
    client = httpx.Client(transport=httpx.MockTransport(fleet.handler),
                          base_url="http://fleet.test")
    rt = EventBusRuntime(fleet_api_url="http://fleet.test",
                         operator_token=OPERATOR, client=client,
                         command_timeout=3.0, poll_interval=0.01,
                         fleet_spend_ttl=60.0)
    fleet.add_instance("acme", "prod-1")
    first = rt.fleet_spend()
    issued = fleet._n
    # A second call inside the TTL is served from cache: no new command issued.
    second = rt.fleet_spend()
    assert second == first
    assert fleet._n == issued
    # Expiring the cache re-fans out.
    rt._fleet_spend_cache = None
    rt.fleet_spend()
    assert fleet._n > issued
