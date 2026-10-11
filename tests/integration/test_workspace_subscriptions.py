"""Workspace-owned subscriptions against the REAL local event server (#952).

The risk in this change lives in the real server: identity preservation across
a live `replace`, the grant check that rejects an ungranted topic, and
cross-session isolation when every session re-composes its own set. These tests
drive the real CLI, the real manager health route and the real
`_start_event_subscription` against a server started from the real sources.

They deliberately do NOT boot a full manager process: the seam under test is the
session that owns a deployment re-applying its own set, and driving that
in-process reaches the same code the manager runs while keeping the test
deterministic.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bobi import manager_health, paths
from bobi.cli import main
from bobi.events.subscriptions import (
    WorkspaceSubscriptionsError,
    render_workspace_subscriptions,
    workspace_subscriptions,
    workspace_subscriptions_path,
)
from bobi.subagent import (
    Subscription,
    _start_event_subscription,
    get_live_subscription,
)

# The real-server fixture lives in test_event_server.py (module-scoped, so
# this module gets its own server), along with the local_only skip for the
# wrangler backend.
from .test_event_server import (  # noqa: F401
    TEST_GRANTS_SECRET,
    _skip_local_only_on_wrangler,
    event_server,
)

GRANTED = "github:test-org/test-repo"
GRANTED_2 = "linear:TEST"


def _server_deployment(base_url: str, deployment_id: str) -> dict:
    """Read a deployment's stored subscription set off the real server."""
    with urllib.request.urlopen(f"{base_url}/health", timeout=5) as resp:
        json.loads(resp.read())
    req = urllib.request.Request(
        f"{base_url}/__test/deployments/{deployment_id}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        return {}


@pytest.fixture
def runtime(event_server, tmp_path, monkeypatch):
    """An installed runtime pointed at the real server, grants auto-seeded."""
    base_url, _port, _backend = event_server
    root = tmp_path / "home" / "agents" / "live-team" / "run"
    paths.package_dir(root).mkdir(parents=True)
    paths.workspace_dir(root).mkdir(parents=True)
    paths.state_dir(root)
    paths.agent_yaml_path(root).write_text(
        "agent: live-team\nentry_point: manager\n"
        f"event_server: {base_url}\n"
        f"subscribe:\n  - {GRANTED}\n"
    )
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOBI_ROOT", str(root))
    # Grants come from the server's test-only route rather than live
    # GitHub/Linear credentials.
    monkeypatch.setenv("BOBI_ES_TEST_GRANTS_SECRET", TEST_GRANTS_SECRET)
    paths.bind_root(None)
    paths.bind_root(root)
    yield base_url, root
    paths.bind_root(None)


def _manager(root) -> str:
    from bobi.service import manager_session_name
    return manager_session_name(root)


def _declare(root, topics):
    path = workspace_subscriptions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workspace_subscriptions(topics))


def _cli(args):
    return CliRunner().invoke(main, ["agent", "live-team", "subscriptions"]
                              + args)


class _Started:
    """A real subscription against the real server, torn down on exit."""

    def __init__(self, root, session, extra=()):
        self.root = root
        self.session = session
        self.extra = list(extra)
        self.sub: Subscription | None = None

    def __enter__(self):
        with patch("bobi.events.drain.drain_loop"):
            self.sub = _start_event_subscription(
                self.session, self.extra, self.root)
        return get_live_subscription(self.session)

    def __exit__(self, *exc):
        if self.sub is not None:
            self.sub.stop(timeout=2)


@pytest.mark.local_only
def test_a_cli_add_and_remove_apply_live_without_restart(runtime):
    """D1 and D3, end to end: the server's stored set changes, the identity does
    not. No `register()` happens, so replay continuity is preserved."""
    base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as controller:
        dep_before, key_before = controller.deployment_id, controller.api_key
        assert GRANTED in controller.accepted
        assert GRANTED_2 not in controller.accepted

        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            added = _cli(["add", GRANTED_2])
            assert added.exit_code == 0, added.output
            assert "Applied live at" in added.output
            assert GRANTED_2 in controller.accepted

            removed = _cli(["remove", GRANTED_2])
            assert removed.exit_code == 0, removed.output
            assert GRANTED_2 not in controller.accepted
        finally:
            manager_health.stop()

        # Identity preserved across both live applies.
        assert (controller.deployment_id, controller.api_key) == (
            dep_before, key_before)
        assert workspace_subscriptions(root) == [GRANTED]
        assert base_url


@pytest.mark.local_only
def test_the_derived_keys_survive_every_recompose(runtime):
    """After an `add`, the live set still carries `inbox/<manager>` and both
    forms of both lifecycle keys.

    A PUT built from the workspace list ALONE would unsubscribe the manager from
    sub-agent completions and from its own inbox, which is what makes it
    addressable by `bobi agent <name> message` and `ask`.
    """
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as controller:
        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            assert _cli(["add", GRANTED_2]).exit_code == 0
        finally:
            manager_health.stop()

        live = controller.accepted
        assert f"inbox/{session}" in live
        for key in ("session.completed", "agent/session.completed",
                    "session.failed", "agent/session.failed"):
            assert key in live
        assert port


@pytest.mark.local_only
def test_a_worker_never_gains_the_managers_topics_across_a_recompose(runtime):
    """Cross-session isolation survives recomposition.

    Per-session deployments exist precisely to prevent this: the 2026-06-12
    incident was project leads receiving the user's Slack DMs to the director.
    Both sessions run against one real server, and the worker re-asserts its own
    set while the manager's declared list holds a `github:` topic.
    """
    base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as manager_controller:
        with _Started(root, "worker-1", ["inbox/worker-1"]) as worker:
            assert worker.accepted == ["inbox/worker-1"]
            worker.reload(authorize=False)
            assert worker.accepted == ["inbox/worker-1"]
            assert GRANTED not in worker.accepted
            # Two distinct deployments, never shared.
            assert worker.deployment_id != manager_controller.deployment_id
        assert GRANTED in manager_controller.accepted
    assert base_url


@pytest.mark.local_only
def test_an_ungranted_topic_is_rejected_and_the_change_is_kept(runtime):
    """D7 end to end, against the real grant check.

    `handleUpdateSubscriptions` rejects the WHOLE update before any write, which
    `event-server/test/core.spec.ts` asserts only for the register path. The
    previously accepted set therefore still routes, and the CLI reports the
    exact topic list the server named.
    """
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])
    ungranted = "github:test-org/never-granted"

    with _Started(root, session) as controller:
        accepted_before = list(controller.accepted)
        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            # Deny the grant seed for this one topic so the server's #488 check
            # is what rejects it, not a local filter.
            import bobi.events.server as es

            real_seed = es._seed_test_resource_grant

            def _seed(base, service, resource, bid, bkey):
                if f"{service}:{resource}" == ungranted:
                    return False
                return real_seed(base, service, resource, bid, bkey)

            with patch.object(es, "_seed_test_resource_grant", _seed):
                result = _cli(["add", ungranted])
        finally:
            manager_health.stop()

        assert result.exit_code != 0
        assert ungranted in result.output
        assert "rejected these topics as unauthorized" in result.output
        # The workspace change is KEPT (D7), and routing is untouched.
        assert ungranted in workspace_subscriptions(root)
        assert controller.accepted == accepted_before
        assert port


@pytest.mark.local_only
def test_a_hand_written_inbox_topic_blocks_the_apply_rather_than_routing(runtime):
    """The only path the CLI's own argument check cannot cover.

    `<run>/workspace/` is writable by design and is advertised to the agent
    brain as its own scratch space, and the server treats `inbox/` as an
    ordinary bubble-scoped topic needing no grant. Enforcing the ban in the
    READER makes every path fail closed: no PUT goes out, and the worker's
    private messages never reach the manager.
    """
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as controller:
        accepted_before = list(controller.accepted)
        path = workspace_subscriptions_path(root)
        for forbidden in ("inbox/worker-1", "reply/x"):
            path.write_text(
                "subscribe:\n  - %s\n  - %s\n" % (GRANTED, forbidden))
            with pytest.raises(WorkspaceSubscriptionsError) as exc:
                controller.reload(authorize=False)
            assert forbidden in str(exc.value)
            assert str(path) in str(exc.value)
            # No PUT landed: the live set is unchanged.
            assert controller.accepted == accepted_before


@pytest.mark.local_only
def test_a_reload_also_converges_monitor_drift(runtime):
    """D12: one PUT applies the topic AND drops a paused monitor's keys, and the
    command's output names both changes."""
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])
    monitors_yaml = paths.package_dir(root) / "monitors.yaml"

    def _write_monitor(enabled: bool) -> None:
        monitors_yaml.write_text(
            "monitors:\n  - name: drifty\n"
            "    event: monitor/drifty.finding\n"
            "    interval: 1h\n"
            f"    enabled: {str(enabled).lower()}\n"
            "    prompt: check\n"
        )

    _write_monitor(True)

    with _Started(root, session) as controller:
        assert "drifty.finding" in controller.accepted
        # The monitor is paused after boot. A reload converges it, which is
        # what D12 accepts: convergence toward what the next restart would do.
        _write_monitor(False)
        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            result = _cli(["add", GRANTED_2])
        finally:
            manager_health.stop()

        assert result.exit_code == 0, result.output
        assert GRANTED_2 in result.output
        assert "drifty.finding" in result.output   # named as removed
        assert "drifty.finding" not in controller.accepted
        assert GRANTED_2 in controller.accepted
        assert port


@pytest.mark.local_only
def test_two_concurrent_adds_both_land_and_both_route(runtime):
    """From a barrier where both commands read the same initial file.

    The manager RE-READS the file rather than applying a caller's delta, so
    whichever reload runs after the second write PUTs both topics and the other
    is a no-op at the server.
    """
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as controller:
        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            start = threading.Barrier(2)
            codes = {}

            def _add(topic):
                start.wait(20)
                codes[topic] = _cli(["add", topic]).exit_code

            topics = [GRANTED_2, "github:test-org/second-repo"]
            threads = [threading.Thread(target=_add, args=(t,)) for t in topics]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            manager_health.stop()

        assert codes == {t: 0 for t in topics}
        declared = workspace_subscriptions(root)
        for topic in topics:
            assert topic in declared
            assert topic in controller.accepted
        assert port


@pytest.mark.local_only
def test_the_cli_against_a_booting_manager_persists_and_exits_zero(runtime):
    """`409` means no controller is registered at all, which is the honest case.

    The boot's own compose then reads the file the CLI wrote, so the change
    still applies at that boot. The version that composed at
    `bobi/service.py` - before the session existed - lost the topic.
    """
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    # Health server up, session not yet started: no live subscription.
    port = manager_health.start(paths.state_dir(root), "live-team",
                                session_status_fn=lambda: [],
                                manager_session=session)
    try:
        result = _cli(["add", GRANTED_2])
        assert result.exit_code == 0, result.output
        assert "no live subscription yet" in result.output
    finally:
        manager_health.stop()

    assert GRANTED_2 in workspace_subscriptions(root)
    with _Started(root, session) as controller:
        # That boot's own compose carried the change.
        assert GRANTED_2 in controller.accepted


@pytest.mark.local_only
def test_list_reads_the_accepted_set_off_the_running_manager(runtime):
    """D4 and D11: `list` reads the last-accepted set out of the running
    manager's memory and flags declared-but-not-live in one direction only."""
    _base_url, root = runtime
    session = _manager(root)
    _declare(root, [GRANTED])

    with _Started(root, session) as controller:
        port = manager_health.start(paths.state_dir(root), "live-team",
                                    session_status_fn=lambda: [],
                                    manager_session=session)
        try:
            result = _cli(["list"])
            assert result.exit_code == 0, result.output
            assert "Last accepted at" in result.output
            assert GRANTED in result.output
            # Healthy team: derived keys are live and never in the file, so the
            # gap section must not fire.
            assert "Declared but NOT live" not in result.output

            # A topic declared but not applied IS flagged.
            _declare(root, [GRANTED, "github:test-org/not-applied"])
            flagged = _cli(["list"])
            assert "Declared but NOT live" in flagged.output
            assert "github:test-org/not-applied" in flagged.output
        finally:
            manager_health.stop()
        assert controller.accepted
        assert port


@pytest.mark.local_only
def test_a_persistent_workers_extra_topic_survives_a_deaf_reconnect(runtime):
    """`bobi agent <name> spawn --subscribe` is a supported way to launch a
    worker on an external topic, so `extra` applies to EVERY session.

    Proves the extras survive the composition move for a non-manager session,
    and that the worker never gains a `github:` topic that is only in the
    manager's workspace file.
    """
    _base_url, root = runtime
    _declare(root, ["github:test-org/manager-only"])

    with patch("bobi.events.drain.drain_loop"):
        with patch("bobi.events.client.EventServerClient") as mock_client:
            sub = _start_event_subscription("worker-2", [GRANTED], root)
            try:
                controller = get_live_subscription("worker-2")
                assert controller.accepted == ["inbox/worker-2", GRANTED]
                mock_client.call_args.kwargs["on_deaf_reconnect"]()
                assert controller.accepted == ["inbox/worker-2", GRANTED]
                assert "github:test-org/manager-only" not in controller.accepted
            finally:
                sub.stop(timeout=2)
    assert os.environ["BOBI_ROOT"]
