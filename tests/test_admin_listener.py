"""Unit tests for the inbound admin command listener (#9).

Dispatch is exercised without a real broker: a fake supervisor records the
requests, a fake telemetry supplies identity + the last snapshot, and an
injected publish captures the command_result the listener emits. start() is
driven through injected register/client seams so the subscription wiring is
covered too.
"""

import time

from bobi.supervisor.admin import AdminListener, admin_topic


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeSupervisor:
    def __init__(self):
        self.calls = []

    def request_manager_restart(self):
        self.calls.append("restart")

    def request_manager_stop(self):
        self.calls.append("stop")

    def request_manager_start(self):
        self.calls.append("start")


class FakeTelemetry:
    def __init__(self, snapshot=None):
        self.identity = {"fleet": "acme", "instance": "prod-1"}
        self.last_snapshot = snapshot


def _listener(supervisor=None, telemetry=None, published=None):
    published = published if published is not None else []

    def publish(topic, source, data, project_root):
        published.append((topic, source, data))
        return True

    listener = AdminListener(
        supervisor=supervisor or FakeSupervisor(),
        telemetry=telemetry or FakeTelemetry(),
        publish_fn=publish,
    )
    listener._published = published
    return listener


def _event(command, command_id="c1", **extra):
    return {"payload": {"command_id": command_id, "command": command, **extra}}


# --- topic contract -------------------------------------------------------

def test_topic_matches_server_contract():
    listener = _listener()
    assert listener.topic == "fleet/admin/acme/prod-1"
    assert admin_topic("f", "i") == "fleet/admin/f/i"


# --- dispatch -------------------------------------------------------------

def test_restart_command_requests_restart_and_acks():
    sup = FakeSupervisor()
    listener = _listener(supervisor=sup)
    listener._dispatch(_event("restart"))
    assert sup.calls == ["restart"]
    topic, source, data = listener._published[0]
    assert topic == "fleet/command_result"
    assert source == "fleet"
    assert data["command_id"] == "c1"
    assert data["status"] == "done"
    assert data["result"]["accepted"] is True
    assert data["deployment"]["instance"] == "prod-1"


def test_stop_and_start_commands():
    sup = FakeSupervisor()
    listener = _listener(supervisor=sup)
    listener._dispatch(_event("stop", command_id="s1"))
    listener._dispatch(_event("start", command_id="s2"))
    assert sup.calls == ["stop", "start"]
    assert [p[2]["status"] for p in listener._published] == ["done", "done"]


def test_status_command_returns_snapshot():
    snap = {"manager": {"status": "idle"}, "deployment": {"instance": "prod-1"}}
    listener = _listener(telemetry=FakeTelemetry(snapshot=snap))
    listener._dispatch(_event("status"))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == snap


def test_status_before_first_heartbeat_is_safe():
    listener = _listener(telemetry=FakeTelemetry(snapshot=None))
    listener._dispatch(_event("status"))
    assert listener._published[0][2]["result"] == {"status": "starting"}


# --- Phase C: tier-3 reads (transcript/roster) + data-plane chat ----------

def test_transcript_command_returns_messages(monkeypatch):
    listener = _listener()
    msgs = [{"role": "user", "text": "ping"},
            {"role": "agent", "text": "stub ack: ping"}]
    monkeypatch.setattr(listener, "_read_transcript", lambda session: msgs)
    listener._dispatch(_event("transcript", command_id="t1",
                              args={"session": "bobi-acme-manager"}))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == {"messages": msgs}


def test_roster_command_returns_subagents(monkeypatch):
    listener = _listener()
    roster = [{"name": "bobi-acme-manager", "role": "manager", "is_manager": True}]
    monkeypatch.setattr(listener, "_read_roster", lambda: roster)
    listener._dispatch(_event("roster", command_id="r1"))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == {"subagents": roster}


def test_spend_command_returns_the_cost_summary(monkeypatch):
    listener = _listener()
    spend = {"total_cost_usd": 1.25, "sessions_counted": 3,
             "by_provider": {"anthropic": 1.25}, "by_model": {},
             "by_session": {}, "by_role": {"manager": 1.25}}
    monkeypatch.setattr(listener, "_read_spend", lambda: spend)
    listener._dispatch(_event("spend", command_id="sp1"))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == {"spend": spend}


def test_usage_command_returns_the_rolling_summary(monkeypatch):
    listener = _listener()
    usage = {
        "window": {"seconds": 259200},
        "jobs": {"total": 4, "completed": 3, "failed": 1, "closed": 0},
        "tokens": {"input": 1000, "cached_input": 200,
                   "output": 300, "total": 1300},
        "cost_usd": 1.25,
        "estimated_cost_usd": 0.0,
    }
    monkeypatch.setattr(listener, "_read_usage", lambda args: usage)
    listener._dispatch(_event("usage", command_id="u1",
                              args={"window_seconds": 259200}))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == {"usage": usage}


def test_usage_command_requires_a_positive_window():
    listener = _listener()
    listener._dispatch(_event("usage", command_id="u1", args={}))
    data = listener._published[0][2]
    assert data["status"] == "error"
    assert data["result"]["code"] == "bad_request"
    assert "window_seconds" in data["error"]


def test_session_log_command_returns_history(monkeypatch):
    listener = _listener()
    log_result = {"sessions": [{"name": "boom", "status": "failed",
                                "error": "turn errored"}],
                  "counts": {"active": 0, "completed": 0,
                             "failed": 1, "crashed": 0},
                  "truncated": False}
    monkeypatch.setattr(listener, "_read_session_log", lambda: log_result)
    listener._dispatch(_event("session_log", command_id="sl1"))
    _topic, _source, data = listener._published[0]
    assert data["status"] == "done"
    assert data["result"] == log_result


def test_read_session_log_folds_history(tmp_path, monkeypatch):
    """The real fold over a temp $BOBI_HOME: newest first, outcome fields
    carried, dead-pid actives read crashed (the MDS-65 honest-status
    mechanism), counts bucketed."""
    import os

    import bobi.service as service_mod
    from bobi.sdk import SessionEntry, SessionRegistry

    monkeypatch.setattr(service_mod, "manager_session_name", lambda root: "mgr")
    reg = SessionRegistry(tmp_path)
    reg.register(SessionEntry(name="mgr", role="manager", status="idle",
                              pid=os.getpid(), last_activity=300.0))
    reg.register(SessionEntry(name="boom", role="engineer", status="failed",
                              error="turn errored", terminal_at=200.0,
                              last_activity=200.0, session_id="sid-boom"))
    reg.register(SessionEntry(name="zombie", role="engineer", status="running",
                              pid=999999999, last_activity=100.0))
    reg.register(SessionEntry(name="curator", role="curator", status="error",
                              last_activity=50.0))

    listener = _listener()
    listener.project_root = tmp_path
    result = listener._read_session_log()

    # the reap keeps the snapshot's own last_activity, so order is by the
    # sessions' real activity, not the observation time
    assert [s["name"] for s in result["sessions"]] == \
        ["mgr", "boom", "zombie", "curator"]
    rows = {s["name"]: s for s in result["sessions"]}
    assert rows["mgr"]["is_manager"] is True
    assert rows["mgr"]["terminal_at"] is None     # null, never omitted
    assert rows["mgr"]["ended"] is False
    assert rows["boom"]["status"] == "failed"
    assert rows["boom"]["error"] == "turn errored"
    assert rows["boom"]["terminal_at"] == 200.0
    assert rows["boom"]["session_id"] == "sid-boom"
    assert rows["boom"]["ended"] is True
    assert rows["zombie"]["status"] == "crashed"
    assert rows["zombie"]["error"]
    assert rows["zombie"]["ended"] is True
    # durably marked on disk too, not just in the returned snapshot
    assert reg.get("zombie").status == "crashed"
    # "error" (the turn-level failure word) counts as failed
    assert result["counts"] == {"active": 1, "completed": 0,
                                "failed": 2, "crashed": 1}
    assert result["truncated"] is False


def test_read_session_log_caps_newest_first(tmp_path, monkeypatch):
    import bobi.service as service_mod
    from bobi.sdk import SessionEntry, SessionRegistry
    from bobi.supervisor.admin import MAX_SESSION_LOG_ROWS

    monkeypatch.setattr(service_mod, "manager_session_name", lambda root: "mgr")
    reg = SessionRegistry(tmp_path)
    n = MAX_SESSION_LOG_ROWS + 5
    for i in range(n):
        reg.register(SessionEntry(name=f"run-{i:03d}", status="completed",
                                  terminal_at=float(i + 1),
                                  last_activity=float(i + 1)))

    listener = _listener()
    listener.project_root = tmp_path
    result = listener._read_session_log()

    assert len(result["sessions"]) == MAX_SESSION_LOG_ROWS
    assert result["truncated"] is True
    # counts cover the WHOLE history, not the capped page
    assert result["counts"]["completed"] == n
    # the cap keeps the recent end
    assert result["sessions"][0]["name"] == f"run-{n - 1:03d}"
    assert result["sessions"][-1]["name"] == f"run-{n - MAX_SESSION_LOG_ROWS:03d}"


def test_chat_command_runs_async_and_publishes_done(monkeypatch):
    calls = {}

    def fake_ask(root, session, text, **kw):
        calls["args"] = (root, session, text)

    import bobi.service as service_mod
    monkeypatch.setattr(service_mod, "ask", fake_ask)
    listener = _listener()
    monkeypatch.setattr(listener, "_manager_session", lambda: "bobi-acme-manager")

    listener._dispatch(_event("chat", command_id="ch1", args={"text": "ping"}))
    assert _wait(lambda: listener._published), "chat never published a result"
    _topic, _source, data = listener._published[0]
    assert data["command_id"] == "ch1"
    assert data["status"] == "done"
    assert data["result"]["delivered"] is True
    assert data["result"]["session"] == "bobi-acme-manager"
    assert calls["args"][1] == "bobi-acme-manager"
    assert calls["args"][2] == "ping"


def test_chat_does_not_block_the_dispatch_worker(monkeypatch):
    """A minutes-long turn must not stall dispatch: `_dispatch` returns while the
    ask is still in flight (no result yet), and a queued restart is unaffected."""
    import threading

    gate = threading.Event()

    def blocking_ask(root, session, text, **kw):
        gate.wait(5)

    import bobi.service as service_mod
    monkeypatch.setattr(service_mod, "ask", blocking_ask)
    sup = FakeSupervisor()
    listener = _listener(supervisor=sup)
    monkeypatch.setattr(listener, "_manager_session", lambda: "mgr")

    listener._dispatch(_event("chat", command_id="ch1", args={"text": "hi"}))
    # The chat turn is still blocked in ask, yet dispatch already returned:
    # no result published, and a following restart runs to completion.
    assert listener._published == []
    listener._dispatch(_event("restart", command_id="rs1"))
    assert sup.calls == ["restart"]
    assert listener._published[0][2]["command_id"] == "rs1"

    gate.set()
    assert _wait(lambda: len(listener._published) == 2), "chat never resolved"
    chat_result = [p[2] for p in listener._published if p[2]["command_id"] == "ch1"]
    assert chat_result and chat_result[0]["status"] == "done"


def test_chat_malformed_args_resolves_and_never_leaks_pending(monkeypatch):
    """A non-dict ``args`` (the server does not validate arg shape) must not
    AttributeError into a swallowed dispatch that leaves the command pending
    forever - it coerces to {} and resolves as an error."""
    listener = _listener()
    monkeypatch.setattr(listener, "_manager_session", lambda: "mgr")
    listener._dispatch(_event("chat", command_id="m1", args="not-a-dict"))
    assert _wait(lambda: listener._published), "malformed chat never resolved"
    data = listener._published[0][2]
    assert data["command_id"] == "m1"
    assert data["status"] == "error"


def test_chat_empty_text_resolves_error(monkeypatch):
    listener = _listener()
    monkeypatch.setattr(listener, "_manager_session", lambda: "mgr")
    listener._dispatch(_event("chat", command_id="e1", args={"text": "   "}))
    assert _wait(lambda: listener._published), "empty chat never resolved"
    data = listener._published[0][2]
    assert data["command_id"] == "e1"
    assert data["status"] == "error"
    assert "empty" in (data.get("error") or "")


def test_malformed_command_is_ignored_no_reply():
    sup = FakeSupervisor()
    listener = _listener(supervisor=sup)
    listener._dispatch({"payload": {"command": "restart"}})     # no command_id
    listener._dispatch({"payload": {"command_id": "x"}})          # no command
    listener._dispatch(_event("rm -rf"))                          # unknown verb
    listener._dispatch({})                                        # no payload
    assert sup.calls == []
    assert listener._published == []


def test_dispatch_never_raises_out():
    # A publish that blows up must not escape (_on_event swallows).
    class Boom:
        identity = {"fleet": "f", "instance": "i"}
        last_snapshot = None

    def publish(*_a, **_k):
        raise RuntimeError("broker down")

    listener = AdminListener(supervisor=FakeSupervisor(), telemetry=Boom(),
                             publish_fn=publish)
    listener._safe_dispatch(_event("restart"))  # must not raise


# --- start() wiring -------------------------------------------------------

def test_start_registers_admin_subscription_and_opens_client():
    registered = {}
    started = {"n": 0}

    def register_fn(url, topic):
        registered["url"] = url
        registered["topic"] = topic
        return ("dep-admin", "api-key")

    class FakeClient:
        def __init__(self, *a):
            self.args = a

        def start(self):
            started["n"] += 1

        def stop(self):
            started["n"] -= 1

    def client_factory(url, deployment_id, api_key, on_event):
        return FakeClient(url, deployment_id, api_key, on_event)

    listener = AdminListener(
        supervisor=FakeSupervisor(), telemetry=FakeTelemetry(),
        event_server_url="http://es.local", register_fn=register_fn,
        client_factory=client_factory,
    )
    listener.start()
    assert registered["topic"] == "fleet/admin/acme/prod-1"
    assert registered["url"] == "http://es.local"
    assert started["n"] == 1
    listener.stop()
    assert started["n"] == 0


def test_start_is_fail_open_on_register_error():
    def register_fn(_url, _topic):
        raise RuntimeError("event server unreachable")

    listener = AdminListener(
        supervisor=FakeSupervisor(), telemetry=FakeTelemetry(),
        event_server_url="http://es.local", register_fn=register_fn,
    )
    listener.start()   # must not raise
    listener.stop()    # no client - must not raise
