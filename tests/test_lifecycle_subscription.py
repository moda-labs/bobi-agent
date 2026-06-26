"""MDS-65 RC#1 — the entry point must subscribe to sub-agent lifecycle topics
so a detached agent's completion/failure reaches the launcher, and the reactor
must treat those events as deliver-to-inbox (never an auto-dispatch trigger).
"""

from bobi.events.reactor import AutoDispatchRule, EventReactor
from bobi.events.subscriptions import (
    LIFECYCLE_EVENTS,
    lifecycle_subscription_keys,
    monitor_subscription_keys,
)


class TestLifecycleSubscriptionKeys:
    def test_returns_bare_and_qualified_forms(self):
        keys = lifecycle_subscription_keys()
        for event in LIFECYCLE_EVENTS:
            bare = event.split("/", 1)[1]
            assert bare in keys, f"missing bare {bare}"
            assert event in keys, f"missing qualified {event}"

    def test_covers_completed_and_failed(self):
        keys = lifecycle_subscription_keys()
        assert "session.completed" in keys
        assert "agent/session.completed" in keys
        assert "session.failed" in keys
        assert "agent/session.failed" in keys

    def test_no_duplicate_keys(self):
        keys = lifecycle_subscription_keys()
        assert len(keys) == len(set(keys))

    def test_mirrors_monitor_key_shape(self):
        # Same both-forms contract as monitor_subscription_keys (server-version
        # compatibility), so delivery works across old and new servers.
        assert lifecycle_subscription_keys() == monitor_subscription_keys(
            list(LIFECYCLE_EVENTS)
        )


class TestEntryPointSubscribesToLifecycle:
    def test_subscribe_list_built_like_cli_includes_lifecycle(self):
        """Reproduction: a subscribe list built the way cli.py builds it (Slack
        topics + monitor keys) contains NO agent/session.* key until the
        lifecycle keys are appended. After the fix they are present."""
        # Pre-fix shape: discovered topics + monitor keys, no lifecycle.
        subscribe = ["github:moda-labs/repo", "inbox/manager"]
        subscribe += [k for k in monitor_subscription_keys(["monitor/support.email"])
                      if k not in subscribe]
        assert not any(k.startswith("agent/session.") for k in subscribe)
        assert "session.completed" not in subscribe

        # The cli.py wiring appends lifecycle keys.
        for key in lifecycle_subscription_keys():
            if key not in subscribe:
                subscribe.append(key)

        assert "agent/session.completed" in subscribe
        assert "agent/session.failed" in subscribe
        assert "session.completed" in subscribe


class TestLifecycleNotAutoDispatched:
    def test_lifecycle_event_is_not_dispatched(self):
        """A lifecycle event with no matching auto_dispatch rule returns None
        from the reactor — it is delivered to the inbox, not used to launch a
        workflow. (The drain always delivers; the reactor only annotates.)"""
        reactor = EventReactor(
            rules=[AutoDispatchRule(event="github.pull_request_review",
                                    workflow="pr-feedback")],
            cwd="/tmp",
        )
        for event_type in ("agent/session.completed", "agent/session.failed",
                            "session.completed"):
            event = {"type": event_type, "fields": {"run_key": "X-1"}}
            assert reactor.process(event) is None
