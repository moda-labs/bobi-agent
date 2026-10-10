"""The workspace subscription surface: strict validator, precedence, composer.

`workspace/subscriptions.yaml` is operator-owned and WRITABLE, which is what
makes it need its own validator rather than the pack `subscribe:` parser: under
this design `[]` is an authoritative "subscribe to nothing", and the pack parser
maps six distinct wrong inputs to `[]`. These tests pin that the validator
rejects each one by name, that an invalid file never falls through to
auto-detection, and that the composer keys its manager-only layers off the
session name (#952).
"""

import pytest
import yaml

from bobi import paths
from bobi.events.subscriptions import (
    WorkspaceSubscriptionsError,
    compose_session_subscriptions,
    discover_subscriptions,
    explicit_subscriptions,
    render_workspace_subscriptions,
    workspace_subscriptions,
    workspace_subscriptions_path,
)


def _write_workspace(root, text: str):
    path = workspace_subscriptions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# Each case is (yaml text, the substring the error must name). The reason string
# is asserted because an operator's only repair signal is the message.
INVALID_CASES = [
    ("subscribe: [a\n", "not valid YAML"),
    ("", "is empty"),
    ("{}\n", "no `subscribe:` key"),
    ("other: 1\n", "unknown top-level key"),
    ("- github:o/r\n", "document root is a list"),
    ("subscribe: github:o/r\n", "`subscribe:` is a str"),
    ("subscribe: {}\n", "`subscribe:` is a dict"),
    ("subscribe:\n  - 7\n", "not a string"),
    ("subscribe:\n  - '  '\n", "is empty"),
    ("subscribe:\n  - github:o/r\n  - github:o/r\n", "duplicate topic"),
    ("subscribe:\n  - inbox/worker-1\n", "reserved 'inbox/' namespace"),
    ("subscribe:\n  - reply/x\n", "reserved 'reply/' namespace"),
]


@pytest.mark.parametrize("text,reason", INVALID_CASES)
def test_the_strict_validator_rejects_each_wrong_document(
        bobi_install, text, reason):
    root = bobi_install.repo_path
    path = _write_workspace(root, text)
    with pytest.raises(WorkspaceSubscriptionsError) as exc:
        workspace_subscriptions(root)
    assert str(path) in str(exc.value)
    assert reason in str(exc.value)


@pytest.mark.parametrize("text,reason", INVALID_CASES)
def test_an_invalid_file_never_falls_through_to_auto_detection(
        bobi_install, text, reason):
    """Asserted against the REAL detector, not a mock.

    `discover_subscriptions` reads the workspace file BEFORE and OUTSIDE its
    `try`. Inside it, every one of these documents would be swallowed into
    auto-detection, which for a real team collapses four GitHub topics to one
    and promotes a channel-scoped Slack subscription to workspace-wide.
    """
    root = bobi_install.repo_path
    _write_workspace(root, text)
    with pytest.raises(WorkspaceSubscriptionsError):
        discover_subscriptions(root)
    with pytest.raises(WorkspaceSubscriptionsError):
        explicit_subscriptions(root)


def test_an_invalid_file_is_reported_by_doctor_and_survived_by_the_snapshot(
        bobi_install, monkeypatch):
    """The three other readers: doctor must fail, the snapshot must not raise."""
    root = bobi_install.repo_path
    path = _write_workspace(root, "subscribe: [a\n")

    from bobi import doctor
    monkeypatch.setattr(doctor, "bound_root", lambda: root)
    result = doctor._check_ingress_reachability()
    assert result.ok is False
    assert str(path) in (result.hint or "")

    from bobi.supervisor.snapshot import _expectations
    assert _expectations(root)["subscriptions"] == []


def test_absent_file_reads_as_none_and_changes_nothing(bobi_install):
    root = bobi_install.repo_path
    assert workspace_subscriptions(root) is None
    # The pack has no subscribe:, so discovery still auto-detects exactly as
    # before this layer existed. No new file, no boot reading differently.
    assert discover_subscriptions(root) == [root.name]


def test_an_empty_declared_list_is_authoritative(bobi_install):
    """`subscribe: []` means nothing, NOT "auto-detect from git remotes".

    Red against the pack reader's `if explicit:` truthiness gate, which is why
    the workspace layer is consulted before it rather than through it.
    """
    root = bobi_install.repo_path
    _write_workspace(root, "subscribe: []\n")
    assert workspace_subscriptions(root) == []
    assert explicit_subscriptions(root) == []
    assert discover_subscriptions(root) == []


def test_a_declared_list_wins_over_the_pack_seed_for_both_readers(bobi_install):
    """The D078 one-parser invariant: ingress and discovery agree.

    `bobi/ingress.py` reads `explicit_subscriptions`, so a layer added only to
    `discover_subscriptions` would make the reachability warning name the pack's
    topics while the session subscribed to the operator's.
    """
    root = bobi_install.repo_path
    paths.agent_yaml_path(root).write_text(
        "agent: test-agent\nentry_point: director\n"
        "subscribe:\n  - github:pack/seed\n"
    )
    assert explicit_subscriptions(root) == ["github:pack/seed"]

    _write_workspace(root, "subscribe:\n  - github:o/declared\n")
    assert explicit_subscriptions(root) == ["github:o/declared"]
    assert discover_subscriptions(root) == ["github:o/declared"]


def test_a_rendered_file_round_trips_through_the_validator(bobi_install):
    """What the CLI writes is what the reader accepts, header comment included."""
    root = bobi_install.repo_path
    topics = ["github:o/r", "linear:MOD", "slack:T1:app:A1:C1"]
    text = render_workspace_subscriptions(topics)
    assert text.startswith("# Event topics this team subscribes to.")
    _write_workspace(root, text)
    assert workspace_subscriptions(root) == topics
    assert yaml.safe_load(text) == {"subscribe": topics}


def test_compose_for_a_worker_is_inbox_plus_extras_and_makes_no_network_call(
        bobi_install, monkeypatch):
    """The regression guard for every existing direct caller.

    Composing the manager's layers for a worker would push the manager's
    `github:`/`slack:`/`linear:` topics onto that worker's own deployment, which
    is the 2026-06-12 incident where project leads received the user's Slack DMs
    to the director. Asserted against the real detector: the manager-name
    compare must fail FIRST, so no detection runs at all.
    """
    root = bobi_install.repo_path
    _write_workspace(root, "subscribe:\n  - github:o/declared\n")

    from bobi.events import adapters

    def _no_network(*a, **k):
        raise AssertionError("a worker's composition must not auto-detect")

    monkeypatch.setattr(adapters, "detect", _no_network)

    keys = compose_session_subscriptions(root, "worker-1", ["github:o/extra"])
    assert keys == ["inbox/worker-1", "github:o/extra"]
    assert not [k for k in keys if k == "github:o/declared"]
    assert "session.completed" not in keys


def test_compose_for_the_manager_layers_declared_monitors_and_lifecycle(
        bobi_install):
    """And `extra` is applied for EVERY session, manager included."""
    from bobi.service import manager_session_name

    root = bobi_install.repo_path
    _write_workspace(root, "subscribe:\n  - github:o/declared\n")
    session = manager_session_name(root)

    keys = compose_session_subscriptions(root, session, ["github:o/extra"])
    assert keys[0] == f"inbox/{session}"
    assert "github:o/declared" in keys
    assert "github:o/extra" in keys
    # Both forms of both lifecycle keys, which is what keeps delivery working
    # across event-server versions.
    for key in ("session.completed", "agent/session.completed",
                "session.failed", "agent/session.failed"):
        assert key in keys
    assert len(keys) == len(set(keys))


def test_compose_rejects_a_hand_written_forbidden_topic(bobi_install):
    """The hand-edit path the CLI's argument check cannot cover.

    The event server accepts any non-empty string at registration and treats
    `inbox/` as an ordinary bubble-scoped topic, so a hand-written
    `inbox/<worker>` would be composed and PUT on the next reload and would
    route that worker's private messages here.
    """
    from bobi.service import manager_session_name

    root = bobi_install.repo_path
    path = _write_workspace(root, "subscribe:\n  - inbox/worker-1\n")
    with pytest.raises(WorkspaceSubscriptionsError) as exc:
        compose_session_subscriptions(root, manager_session_name(root))
    assert str(path) in str(exc.value)


def test_a_duplicate_subscribe_key_is_rejected(bobi_install):
    """Codex review F5.

    `yaml.safe_load` silently keeps the LAST duplicate mapping key, so this
    document would have parsed as an authoritative `[]` and unsubscribed the
    team from everything - the exact fail-closed hazard the strict validator
    exists for.
    """
    root = bobi_install.repo_path
    path = _write_workspace(
        root, "subscribe:\n  - github:o/keep\nsubscribe: []\n")
    with pytest.raises(WorkspaceSubscriptionsError) as exc:
        workspace_subscriptions(root)
    assert "duplicate key" in str(exc.value)
    assert str(path) in str(exc.value)
    with pytest.raises(WorkspaceSubscriptionsError):
        discover_subscriptions(root)


def test_an_invalid_file_appearing_between_discoverys_two_reads_still_raises(
        bobi_install, monkeypatch):
    """Codex review F6.

    `discover_subscriptions` reads the file, then calls `explicit_subscriptions`
    (which the D078 pin requires) and so reads it AGAIN. A hand edit landing in
    between used to be swallowed by discovery's own `except Exception` and fall
    through to auto-detection.
    """
    root = bobi_install.repo_path
    from bobi.events import subscriptions as mod

    calls = {"n": 0}
    real = mod.workspace_subscriptions

    def _appears_late(project_path):
        calls["n"] += 1
        if calls["n"] == 1:
            return None          # first read: no file
        _write_workspace(root, "subscribe: [a\n")
        return real(project_path)

    monkeypatch.setattr(mod, "workspace_subscriptions", _appears_late)
    with pytest.raises(WorkspaceSubscriptionsError):
        mod.discover_subscriptions(root)
    assert calls["n"] >= 2


def test_a_permissive_pack_seed_is_sanitized_before_it_is_written(bobi_install):
    """Codex review F7.

    The pack parser tolerates a duplicate and an `inbox/` topic that this
    file's reader rejects. Seeding one verbatim would fail every later apply
    with no way for `remove` to repair it, because the argument check refuses
    to name a forbidden topic.
    """
    from bobi.events.subscriptions import sanitize_seed_topics

    kept, dropped = sanitize_seed_topics(
        ["github:o/r", "github:o/r", "inbox/worker-1", "reply/x", "  ",
         "linear:MOD"])
    assert kept == ["github:o/r", "linear:MOD"]
    assert set(dropped) == {"github:o/r", "inbox/worker-1", "reply/x", "  "}
    # And what survives is accepted by the reader it will be written for.
    _write_workspace(bobi_install.repo_path,
                     render_workspace_subscriptions(kept))
    assert workspace_subscriptions(bobi_install.repo_path) == kept
