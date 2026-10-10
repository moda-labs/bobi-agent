"""`bobi agent <name> subscriptions list|add|remove`.

The CLI edits `workspace/subscriptions.yaml` and then asks the running manager
to re-apply it; it never PUTs the event server itself. These tests pin the
argument gate (which reuses the workspace reader's own rule, so the CLI cannot
write a file its own reader rejects), the read-modify-write under the file lock,
and the persist-only path a manager-down edit takes (#952).
"""

import threading

from click.testing import CliRunner
import pytest

from bobi import paths
from bobi.cli import main
from bobi.events.subscriptions import (
    render_workspace_subscriptions,
    workspace_subscriptions,
    workspace_subscriptions_path,
)


@pytest.fixture
def runtime(bobi_install, monkeypatch):
    """A bound runtime with no manager running, so no reload is reachable."""
    root = bobi_install.repo_path
    monkeypatch.setenv("BOBI_ROOT", str(root))
    paths.agent_yaml_path(root).write_text(
        "agent: test-agent\nentry_point: director\n"
        "subscribe:\n  - github:o/seed\n  - linear:MOD\n"
    )
    return root


def _run(args):
    return CliRunner().invoke(main, ["agent", "test-agent", "subscriptions"]
                              + args)


def _declare(root, topics):
    path = workspace_subscriptions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workspace_subscriptions(topics))


@pytest.mark.parametrize("args", [
    ["add", "inbox/worker-1"],
    ["add", "reply/x"],
    ["add", ""],
    ["add", "   "],
    ["add", "github:o/r", "github:o/r"],
    ["remove", "github:o/not-declared"],
    ["remove", "inbox/worker-1"],
])
def test_a_rejected_argument_writes_nothing(runtime, args, monkeypatch):
    """Fails the WHOLE command non-zero with nothing written and no POST."""
    posted = []
    from bobi import cli
    monkeypatch.setattr(cli, "_post_manager_reload",
                        lambda *a, **k: posted.append(a) or None)

    _declare(runtime, ["github:o/kept"])
    result = _run(args)
    assert result.exit_code != 0
    assert workspace_subscriptions(runtime) == ["github:o/kept"]
    assert posted == []


def test_the_first_add_seeds_from_the_effective_set(runtime):
    """A team that was auto-detecting keeps its effective set.

    Without this the file would collapse to the one topic named on the command
    line, which under an authoritative `[]`-means-nothing design silently
    unsubscribes the team from everything else it was hearing.
    """
    assert workspace_subscriptions(runtime) is None
    result = _run(["add", "github:o/new"])
    assert result.exit_code == 0, result.output
    assert workspace_subscriptions(runtime) == [
        "github:o/seed", "linear:MOD", "github:o/new"]
    assert "seeded from the pack list" in result.output
    # Persist-only, because no manager health port file exists.
    assert "not running" in result.output


def test_add_is_idempotent_and_remove_requires_presence(runtime):
    _declare(runtime, ["github:o/one"])
    assert _run(["add", "github:o/one"]).exit_code == 0
    assert workspace_subscriptions(runtime) == ["github:o/one"]

    assert _run(["remove", "github:o/one"]).exit_code == 0
    assert workspace_subscriptions(runtime) == []

    missing = _run(["remove", "github:o/one"])
    assert missing.exit_code != 0
    assert "not subscribed to" in missing.output


def test_what_the_cli_writes_its_own_reader_accepts(runtime):
    _run(["add", "github:o/one", "linear:MDS"])
    # No exception: the written document passes the strict validator, header
    # comment and all.
    assert workspace_subscriptions(runtime) == [
        "github:o/seed", "linear:MOD", "github:o/one", "linear:MDS"]


def test_two_concurrent_adds_compose_instead_of_overwriting(runtime):
    """Both topics land, from a barrier where both read the same initial file.

    The re-read INSIDE `fsutil.file_lock` is what makes this compose. A version
    that reads before taking the lock loses whichever write landed first.
    """
    _declare(runtime, ["github:o/base"])
    start = threading.Barrier(2)
    results = {}

    def _add(topic):
        start.wait(10)
        results[topic] = _run(["add", topic]).exit_code

    threads = [threading.Thread(target=_add, args=(t,))
               for t in ("github:o/x", "github:o/y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert results == {"github:o/x": 0, "github:o/y": 0}
    final = workspace_subscriptions(runtime)
    assert set(final) == {"github:o/base", "github:o/x", "github:o/y"}


def test_list_shows_the_declared_set_and_says_the_manager_is_down(runtime):
    _declare(runtime, ["github:o/one", "linear:MOD"])
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    assert "github:o/one" in result.output
    assert "linear:MOD" in result.output
    assert "Live set unavailable: the manager is not running." in result.output


def test_list_labels_an_unseeded_team_as_pack_or_auto_detected(runtime):
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    assert "no subscriptions.yaml yet" in result.output
    assert "github:o/seed" in result.output


def test_the_reload_timeout_is_derived_from_the_routes_own_bound():
    """Set ABOVE `10s x global topics + 10s`, so a slow authorize cannot make
    the caller give up on an apply that then lands."""
    from bobi.cli import _reload_timeout

    topics = ["inbox/mgr", "session.completed",
              "github:o/a", "github:o/b", "linear:MOD",
              "slack:T1:app:A1:C1"]
    worst_case = 10.0 * 4 + 10.0
    assert _reload_timeout(topics) > worst_case
    assert _reload_timeout([]) > 10.0


def test_a_manager_up_apply_reports_what_changed(runtime, monkeypatch):
    """The reload outcome is read off the response BODY, because a 2xx means the
    manager processed the request while the body says what the EVENT server
    answered."""
    import httpx

    from bobi import cli

    _declare(runtime, ["github:o/one"])
    monkeypatch.setattr(cli, "_manager_reload_endpoint",
                        lambda p: ("http://127.0.0.1:1", "tok"))

    def _fake_post(url, headers=None, timeout=None):
        return httpx.Response(200, json={
            "status": "applied",
            "subscriptions": ["inbox/mgr", "github:o/one", "github:o/two"],
            "added_topics": ["github:o/two"],
            "removed_topics": ["monitor/paused.thing"],
            "at": "2026-10-10T18:22:04+00:00",
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", _fake_post)
    result = _run(["add", "github:o/two"])
    assert result.exit_code == 0, result.output
    assert "Applied live at 2026-10-10T18:22:04+00:00" in result.output
    # Printed in FULL: a reload also converges monitor drift, so the command
    # names more than the topic typed.
    assert "added:   github:o/two" in result.output
    assert "removed: monitor/paused.thing" in result.output


@pytest.mark.parametrize("body,needle", [
    ({"status": "rejected", "error": "unauthorized_topics",
      "topics": ["github:o/ungranted"]}, "github:o/ungranted"),
    ({"status": "rejected", "error": "deployment_auth_failed",
      "http_status": 403}, "does not recognise this deployment"),
    ({"status": "skipped", "reason": "degraded_detection"},
     "narrower set than the live one"),
])
def test_a_rejected_apply_keeps_the_workspace_change_and_exits_non_zero(
        runtime, monkeypatch, body, needle):
    import httpx

    from bobi import cli

    _declare(runtime, ["github:o/one"])
    monkeypatch.setattr(cli, "_manager_reload_endpoint",
                        lambda p: ("http://127.0.0.1:1", "tok"))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, json=body, request=httpx.Request("POST", url)))

    result = _run(["add", "github:o/two"])
    assert result.exit_code != 0
    assert needle in result.output
    # D7: the workspace change is KEPT.
    assert workspace_subscriptions(runtime) == ["github:o/one", "github:o/two"]


def test_a_401_from_the_route_names_the_token_file(runtime, monkeypatch):
    import httpx

    from bobi import cli

    _declare(runtime, ["github:o/one"])
    monkeypatch.setattr(cli, "_manager_reload_endpoint",
                        lambda p: ("http://127.0.0.1:1", "tok"))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        401, json={"error": "bad or missing token"},
        request=httpx.Request("POST", url)))

    result = _run(["add", "github:o/two"])
    assert result.exit_code != 0
    assert "state/manager-health.token" in result.output


def test_a_409_from_the_route_is_persist_only_and_exits_zero(runtime, monkeypatch):
    """A booting manager: the change is persisted and the boot's own compose
    reads the file, so it applies at that boot."""
    import httpx

    from bobi import cli

    _declare(runtime, ["github:o/one"])
    monkeypatch.setattr(cli, "_manager_reload_endpoint",
                        lambda p: ("http://127.0.0.1:1", "tok"))
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        409, json={"status": "no_live_subscription", "session": "mgr"},
        request=httpx.Request("POST", url)))

    result = _run(["add", "github:o/two"])
    assert result.exit_code == 0, result.output
    assert "no live subscription yet" in result.output
    assert "next reload" in result.output
    assert workspace_subscriptions(runtime) == ["github:o/one", "github:o/two"]


def test_the_liveness_test_is_the_port_and_token_files(runtime):
    """Self-evidencing: both are written by `manager_health.start()` and
    unlinked by its `stop()`. No pid probe, no launch stamp, no state table."""
    from bobi.cli import _manager_reload_endpoint

    state = paths.state_dir(runtime)
    assert _manager_reload_endpoint(runtime) is None
    (state / "manager-health.port").write_text("51234")
    assert _manager_reload_endpoint(runtime) is None   # token still missing
    (state / "manager-health.token").write_text("tok")
    assert _manager_reload_endpoint(runtime) == ("http://127.0.0.1:51234", "tok")
