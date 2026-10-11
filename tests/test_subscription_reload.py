"""Re-applying a session's own subscription set: reload, the lock, the guards.

One writer per deployment (#952). The CLI only edits
`workspace/subscriptions.yaml`; the session that OWNS the deployment re-composes
and re-applies. These tests drive the three writing call sites in that process
(boot, reload route, deaf reconnect) and pin the properties that replace the
previous design's cross-process coordination: the lock covers compose AND apply,
the boot window is closed by registration order, and the desired set is always
re-derived rather than replayed from a cache.

The deaf path is entered only after 95s of no pong, so these drive
`on_deaf_reconnect` off a patched client with no wait.
"""

import json
import threading
from unittest.mock import patch

import httpx
import pytest

from bobi import http as pooled
from bobi import paths
from bobi.events.protocol import EVENT_PROTOCOL
from bobi.subagent import (
    _start_event_subscription,
    get_live_subscription,
)

REMOTE_URL = "https://events.example.invalid"

# The manager's composition appends both forms of both lifecycle keys, which is
# what keeps sub-agent completions delivered across event-server versions.
LIFECYCLE = ["session.completed", "agent/session.completed",
             "session.failed", "agent/session.failed"]


@pytest.fixture
def project(tmp_path):
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        f"agent: test\nentry_point: manager\nevent_server: {REMOTE_URL}\n"
    )
    paths.workspace_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_bubble():
    with patch("bobi.events.server.ensure_bubble",
               return_value={"bubble_id": "bub_test", "bubble_key": "bkey_test"}):
        yield


@pytest.fixture(autouse=True)
def _clean_registry():
    """A module-level registry must not leak a controller between tests."""
    from bobi import subagent
    yield
    with subagent._live_subscriptions_lock:
        subagent._live_subscriptions.clear()


def _manager(project) -> str:
    from bobi.service import manager_session_name
    return manager_session_name(project)


def _declare(project, topics):
    from bobi.events.subscriptions import (
        render_workspace_subscriptions, workspace_subscriptions_path,
    )
    path = workspace_subscriptions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workspace_subscriptions(topics))


def _saved_deployment(project, session, dep="dep-9", key="key-9"):
    state = paths.state_path(project) / "deployments" / f"{session}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"deployment_id": dep, "api_key": key}))


def _bubble(project):
    from bobi.events.state import save_bubble_state
    save_bubble_state(project, "bub_test", "bkey_test")


def _replaced(requests):
    return [json.loads(r.content)["replace"]
            for r in requests if r.method == "PUT"]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_deaf_reconnect_recomposes_from_the_file(mock_client, _drain, project):
    """The reconnect PUTs the CURRENT file, not the boot-time list.

    Red against the pre-#952 hook, which replayed the in-process
    `active_subscriptions` cache captured at boot.
    """
    session = _manager(project)
    _declare(project, ["github:o/boot"])
    _saved_deployment(project, session)
    _bubble(project)

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        _declare(project, ["github:o/boot", "github:o/added"])
        mock_client.call_args.kwargs["on_deaf_reconnect"]()

    puts = _replaced(captured)
    assert len(puts) == 2
    assert "github:o/added" not in puts[0]
    assert "github:o/added" in puts[1]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_lost_put_response_is_not_reverted_by_the_next_reconnect(
        mock_client, _drain, project):
    """The revert class this design removes.

    The server commits `replace` and THEN responds, so a lost response used to
    leave the old list cached and the next reconnect replayed it - undoing a
    change the server had already accepted. Re-deriving from the file cannot.
    """
    session = _manager(project)
    _declare(project, ["github:o/boot"])
    _saved_deployment(project, session)
    _bubble(project)

    captured = []
    drop_next = {"on": False}

    def handler(request):
        captured.append(request)
        if request.method == "PUT" and drop_next["on"]:
            drop_next["on"] = False
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        # The CLI's apply lands at the server but its response is lost.
        _declare(project, ["github:o/boot", "github:o/added"])
        drop_next["on"] = True
        with pytest.raises(httpx.ReadTimeout):
            controller.reload(authorize=False)
        # The next reconnect must still assert the NEW set.
        mock_client.call_args.kwargs["on_deaf_reconnect"]()

    assert "github:o/added" in _replaced(captured)[-1]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_workers_reconnect_puts_only_its_own_inbox_and_extras(
        mock_client, _drain, project):
    """With a manager workspace file ALSO on disk.

    Proves the composer keys its manager-only layers off the session name, so
    recomposition can never leak the manager's topics onto a worker's own
    deployment. Replaces the pre-#952 characterization of the filtered cache.
    """
    _declare(project, ["github:o/manager-only"])
    _saved_deployment(project, "worker-1")
    _bubble(project)

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription("worker-1", ["github:o/worker-extra"], project)
        mock_client.call_args.kwargs["on_deaf_reconnect"]()

    for replaced in _replaced(captured):
        assert replaced == ["inbox/worker-1", "github:o/worker-extra"]
        assert "github:o/manager-only" not in replaced


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_the_lock_covers_compose_and_apply_together(mock_client, _drain, project):
    """Driven by a barrier, not by timing.

    Reload A is paused INSIDE the lock after it composes; the file is then
    mutated and the deaf reconnect fired. A version that locks only the PUT
    lands A's stale set last and is caught here.
    """
    session = _manager(project)
    _declare(project, ["github:o/one"])
    _saved_deployment(project, session)
    _bubble(project)

    captured = []
    composed = threading.Event()
    may_put = threading.Event()
    gate_armed = {"on": False}

    def handler(request):
        if request.method == "PUT" and gate_armed["on"]:
            gate_armed["on"] = False
            composed.set()
            # Held INSIDE the lock, between A's compose and A's write.
            may_put.wait(10)
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        on_deaf = mock_client.call_args.kwargs["on_deaf_reconnect"]

        gate_armed["on"] = True
        a = threading.Thread(target=lambda: controller.reload(authorize=False))
        a.start()
        assert composed.wait(10)

        _declare(project, ["github:o/one", "github:o/two"])
        b_done = threading.Event()

        def _b():
            on_deaf()
            b_done.set()

        b = threading.Thread(target=_b)
        b.start()
        # B must NOT have applied while A holds the lock.
        assert not b_done.wait(0.5)
        may_put.set()
        a.join(10)
        b.join(10)

    puts = _replaced(captured)
    # A landed its (stale) set first, B landed the newest file contents last.
    assert puts[-1] == ["inbox/" + session, "github:o/one",
                        "github:o/two"] + LIFECYCLE
    assert "github:o/two" not in puts[-2]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_reload_during_boot_blocks_and_then_applies_the_newer_set(
        mock_client, _drain, project):
    """The boot window, closed by registration order rather than detected.

    The controller is published before the boot's own apply, with the apply lock
    already held, so a reload arriving in that window blocks and then composes
    AGAIN - reading the file the CLI wrote mid-boot. A version that snapshots
    before the session exists loses that topic entirely.
    """
    session = _manager(project)
    _declare(project, ["github:o/boot"])
    _saved_deployment(project, session)
    _bubble(project)

    captured = []
    in_boot_put = threading.Event()
    may_finish_boot = threading.Event()
    reload_done = threading.Event()
    seen_boot = {"on": False}

    def handler(request):
        captured.append(request)
        if request.method == "PUT" and not seen_boot["on"]:
            seen_boot["on"] = True
            in_boot_put.set()
            may_finish_boot.wait(10)
        return httpx.Response(200, json={"ok": True})

    def _boot():
        with patch.object(pooled, "_client",
                          httpx.Client(transport=httpx.MockTransport(handler))):
            _start_event_subscription(session, [], project)
            may_finish_boot.wait(10)

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        boot = threading.Thread(target=_boot)
        boot.start()
        assert in_boot_put.wait(10)

        # The controller is registered even though the boot has not finished.
        controller = get_live_subscription(session)
        assert controller is not None
        _declare(project, ["github:o/boot", "github:o/mid-boot"])

        def _reload():
            controller.reload(authorize=False)
            reload_done.set()

        t = threading.Thread(target=_reload)
        t.start()
        assert not reload_done.wait(0.5)   # blocked on the boot's apply
        may_finish_boot.set()
        assert reload_done.wait(10)
        boot.join(10)
        t.join(10)

    assert "github:o/mid-boot" in _replaced(captured)[-1]


def test_no_controller_at_all_is_the_honest_409_case(project):
    """`409 no_live_subscription` is left for the case where nothing registered."""
    assert get_live_subscription(_manager(project)) is None


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_torn_down_session_stops_answering_reloads(mock_client, _drain, project):
    session = _manager(project)
    _declare(project, ["github:o/one"])
    _saved_deployment(project, session)
    _bubble(project)

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        sub = _start_event_subscription(session, [], project)
        assert get_live_subscription(session) is not None
        sub.stop(timeout=0.1)
    assert get_live_subscription(session) is None


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_degraded_detection_refuses_to_shrink_a_team_with_no_file(
        mock_client, _drain, project):
    """Auto-detection swallows a transient API failure into a NARROWER set.

    On a team with no workspace file that would let a transport-recovery
    reconnect silently stop routing until the next successful apply. The guard
    is what keeps routing alone, and it logs so the operator can see it.
    """
    session = _manager(project)
    paths.agent_yaml_path(project).write_text(
        "agent: test\nentry_point: manager\n"
        f"event_server: {REMOTE_URL}\n"
        "subscribe:\n  - github:o/one\n  - github:o/two\n"
    )
    _saved_deployment(project, session)
    _bubble(project)

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        before = len(_replaced(captured))
        # Detection degrades: the pack list shrinks under the manager's feet.
        paths.agent_yaml_path(project).write_text(
            "agent: test\nentry_point: manager\n"
            f"event_server: {REMOTE_URL}\n"
            "subscribe:\n  - github:o/one\n"
        )
        body = controller.reload(authorize=False)

    assert body == {
        "status": "skipped", "reason": "degraded_detection",
        "subscriptions": [f"inbox/{session}", "github:o/one"] + LIFECYCLE,
    }
    assert len(_replaced(captured)) == before   # no PUT at all


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_shrink_with_a_workspace_file_is_a_legitimate_removal(
        mock_client, _drain, project):
    session = _manager(project)
    _declare(project, ["github:o/one", "github:o/two"])
    _saved_deployment(project, session)
    _bubble(project)

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        _declare(project, ["github:o/one"])
        body = controller.reload(authorize=False)

    assert body["status"] == "applied"
    assert body["removed_topics"] == ["github:o/two"]
    assert _replaced(captured)[-1] == [
        f"inbox/{session}", "github:o/one"] + LIFECYCLE


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_unauthorized_topics_reaches_the_caller_with_its_topic_list(
        mock_client, _drain, project, caplog):
    """The register path has parsed this 400 shape since #488; the PUT path did
    not - `raise_for_status` discarded the body before anyone read it.

    Also pins that the boot classifier still reports the HTTP-400 diagnosis
    rather than falling through to its catch-all, and that the deaf path logs
    the voided repair at `warning` rather than `debug`.
    """
    from bobi.events.server import UnauthorizedTopics

    session = _manager(project)
    _declare(project, ["github:o/ungranted"])
    _saved_deployment(project, session)
    _bubble(project)

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(400, json={
                "error": "unauthorized_topics",
                "topics": ["github:o/ungranted"],
            })
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        with caplog.at_level("WARNING"):
            with pytest.raises(RuntimeError) as boot_exc:
                _start_event_subscription(session, [], project)

    assert "HTTP 400" in str(boot_exc.value)
    assert "github:o/ungranted" in str(boot_exc.value)
    # Raised, not swallowed: the topic list is what an operator needs.
    assert UnauthorizedTopics.__name__


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_reload_reports_the_servers_rejection_without_losing_the_topics(
        mock_client, _drain, project):
    session = _manager(project)
    _declare(project, ["github:o/one"])
    _saved_deployment(project, session)
    _bubble(project)

    reject = {"on": False}

    def handler(request):
        if request.method == "PUT" and reject["on"]:
            return httpx.Response(400, json={
                "error": "unauthorized_topics",
                "topics": ["github:o/ungranted"],
            })
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        accepted_before = list(controller.accepted)
        _declare(project, ["github:o/one", "github:o/ungranted"])
        reject["on"] = True
        body = controller.reload(authorize=False)

    assert body == {"status": "rejected", "error": "unauthorized_topics",
                    "topics": ["github:o/ungranted"]}
    # The accepted record is NOT advanced by a rejection.
    assert controller.accepted == accepted_before


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_a_403_is_reported_as_deployment_auth_failed_without_overclaiming(
        mock_client, _drain, project):
    """`authenticateDeployment` returns the same 403 for an evicted deployment,
    a wrong deployment id and a wrong api key, so the route must not claim to
    know which."""
    session = _manager(project)
    _declare(project, ["github:o/one"])
    _saved_deployment(project, session)
    _bubble(project)

    reject = {"on": False}

    def handler(request):
        if request.method == "PUT" and reject["on"]:
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        reject["on"] = True
        body = controller.reload(authorize=False)

    assert body == {"status": "rejected", "error": "deployment_auth_failed",
                    "http_status": 403}


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_the_accepted_record_comes_from_the_servers_echo(
        mock_client, _drain, project):
    """`_put_subscriptions` returns the stored set, because the server's counters
    are numeric and the command has to NAME what changed.

    An older reply answering a bare `{"ok": true}` means the set we PUT is what
    it stored, which is what the nine existing stubs exercise.
    """
    session = _manager(project)
    _declare(project, ["github:o/one"])
    _saved_deployment(project, session)
    _bubble(project)

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(200, json={
                "ok": True, "added": 1, "removed": 0,
                "subscriptions": ["server-said-this"],
            })
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        record = controller.accepted_record()

    assert record["subscriptions"] == ["server-said-this"]
    assert record["session"] == session
    assert record["at"]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_an_applied_reload_names_every_change_not_just_the_topic_typed(
        mock_client, _drain, project):
    """A reload also converges monitor drift, so the diff is computed by the
    manager against its own accepted record - the server returns only counters."""
    session = _manager(project)
    _declare(project, ["github:o/one", "github:o/two"])
    _saved_deployment(project, session)
    _bubble(project)

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        _declare(project, ["github:o/one", "github:o/three"])
        body = controller.reload(authorize=False)

    assert body["status"] == "applied"
    assert body["added_topics"] == ["github:o/three"]
    assert body["removed_topics"] == ["github:o/two"]
    assert EVENT_PROTOCOL


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
def test_degraded_detection_also_catches_a_non_subset_fall_through(
        mock_client, _drain, project):
    """Codex review F1.

    A strict-subset test waves this through. When detection fails on a
    Slack-only team, `discover_subscriptions` falls through to
    `[project_path.name]`, which ADDS a topic: the new set is NOT a subset of
    what was accepted, so the reconnect would have PUT it and dropped Slack
    routing. The guard tests "loses a previously accepted topic" instead.
    """
    session = _manager(project)
    paths.agent_yaml_path(project).write_text(
        "agent: test\nentry_point: manager\n"
        f"event_server: {REMOTE_URL}\n"
        "subscribe:\n  - slack:T1:app:A1:C1\n"
    )
    _saved_deployment(project, session)
    _bubble(project)

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with patch.object(pooled, "_client",
                      httpx.Client(transport=httpx.MockTransport(handler))):
        _start_event_subscription(session, [], project)
        controller = get_live_subscription(session)
        assert "slack:T1:app:A1:C1" in controller.accepted
        before = len(_replaced(captured))

        # Detection degrades to nothing, so discovery falls through to the
        # project directory name - a DIFFERENT topic, not a subset.
        paths.agent_yaml_path(project).write_text(
            "agent: test\nentry_point: manager\n"
            f"event_server: {REMOTE_URL}\n"
        )
        body = controller.reload(authorize=False)

    assert body["status"] == "skipped"
    assert body["reason"] == "degraded_detection"
    assert project.name in body["subscriptions"]       # the fall-through topic
    assert len(_replaced(captured)) == before           # and NO PUT went out
