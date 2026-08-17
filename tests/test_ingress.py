"""Ingress reachability diagnostics."""

import pytest

from bobi import paths


def _write_slack_events_config(project_path, event_server_url):
    paths.agent_yaml_path(project_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        f"event_server_url: {event_server_url}\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
    )


def test_warns_when_external_events_use_default_local_ingress(bobi_install):
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "slack" in warning.detail
    assert "http://localhost:8080" in warning.detail
    assert "public tunnel" in warning.hint
    assert "Socket Mode" in warning.hint


def test_warns_for_scheme_less_localhost_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "localhost:8080")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "localhost:8080" in warning.detail


def test_warns_for_loopback_range_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "http://127.0.0.2:8080")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "127.0.0.2" in warning.detail


def test_warns_for_unspecified_bind_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "http://0.0.0.0:8080")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "0.0.0.0" in warning.detail


def test_warns_for_private_ip_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "http://10.0.0.5:8080")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "10.0.0.5" in warning.detail


def test_warns_for_non_global_ip_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "https://100.64.0.1")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "100.64.0.1" in warning.detail


def test_warns_for_public_http_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "http://events.example.com")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "public HTTPS ingress" in warning.detail


def test_warns_for_internal_hostname_ingress(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "https://events.internal")
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "events.internal" in warning.detail


def test_warns_for_explicit_subscribe_without_event_services(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "subscribe:\n"
        "  - github/issues\n"
    )
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "github/issues" in warning.detail


def test_warns_for_slack_prefixed_topic_without_app_token(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "subscribe:\n"
        "  - slack:T_TEAM\n"
    )
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "slack:T_TEAM" in warning.detail


def test_whitespace_slack_app_token_keeps_webhook_warning(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      app_token: '   '\n"
    )
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(bobi_install.repo_path)

    assert warning is not None
    assert "slack" in warning.detail


def test_ignores_outbound_slack_and_discord_services(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      app_token: xapp-configured\n"
        "  - name: discord\n"
        "    events: true\n"
    )
    from bobi.ingress import check_ingress_reachability

    assert check_ingress_reachability(bobi_install.repo_path) is None


def test_ignores_outbound_slack_and_discord_prefixed_topics(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    credentials:\n"
        "      app_token: xapp-configured\n"
        "subscribe:\n"
        "  - slack:T_TEAM\n"
        "  - discord:A_APP\n"
    )
    from bobi.ingress import check_ingress_reachability

    assert check_ingress_reachability(bobi_install.repo_path) is None


def test_mixed_sources_warn_only_for_webhook_transports(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      app_token: xapp-configured\n"
        "  - name: discord\n"
        "    events: true\n"
        "  - name: github\n"
        "    events: true\n"
        "subscribe:\n"
        "  - slack:T_TEAM\n"
        "  - discord:A_APP\n"
        "  - linear/issues\n"
    )
    from bobi.ingress import check_ingress_reachability

    warning = check_ingress_reachability(
        bobi_install.repo_path,
        extra_subscriptions=[
            "slack:T_OTHER", "discord:A_OTHER", "whatsapp:PNID",
        ],
    )

    assert warning is not None
    assert "github" in warning.detail
    assert "linear/issues" in warning.detail
    assert "whatsapp:PNID" in warning.detail
    assert "slack" not in warning.detail
    assert "discord" not in warning.detail


def test_ignores_inbox_only_explicit_subscribe(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "subscribe:\n"
        "  - inbox/test-agent\n"
    )
    from bobi.ingress import check_ingress_reachability

    assert check_ingress_reachability(bobi_install.repo_path) is None


def test_remote_event_server_is_reachable_for_external_events(bobi_install):
    _write_slack_events_config(bobi_install.repo_path, "https://events.example.com")
    from bobi.ingress import check_ingress_reachability

    assert check_ingress_reachability(bobi_install.repo_path) is None


def test_local_ingress_is_ok_without_external_events(bobi_install):
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
    )
    from bobi.ingress import check_ingress_reachability

    assert check_ingress_reachability(bobi_install.repo_path) is None


# --- one parser for `subscribe:` (D078) --------------------------------------


@pytest.mark.parametrize("subscribe_yaml,expected", [
    ("subscribe:\n  - github/issues\n  - linear/MOD\n",
     ["github/issues", "linear/MOD"]),
    ("subscribe: github/issues\n", ["github/issues"]),           # scalar form
    ("subscribe:\n  - '  github/issues  '\n", ["github/issues"]),  # stripped
    ("subscribe:\n  - github/issues\n  - ''\n  - 7\n",
     ["github/issues"]),                                          # empty + non-str
    ("subscribe: {}\n", []),                                      # unsupported shape
    ("", []),                                                     # absent
])
def test_ingress_and_discovery_read_subscribe_identically(
        bobi_install, subscribe_yaml, expected):
    """The ingress warning and the session's real subscriptions share a parser.

    Two copies meant a schema change could make the reachability warning name a
    different set of topics than the session actually subscribed to. Reading
    both through one function is what stops that; this pins them together.
    """
    from bobi.events.subscriptions import discover_subscriptions, explicit_subscriptions

    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\nentry_point: director\n" + subscribe_yaml
    )
    assert explicit_subscriptions(bobi_install.repo_path) == expected
    if expected:
        # An explicit subscribe is discovery's highest-precedence source.
        assert discover_subscriptions(bobi_install.repo_path) == expected


def test_explicit_subscriptions_interpolates_env(bobi_install, monkeypatch):
    from bobi.events.subscriptions import explicit_subscriptions

    monkeypatch.setenv("INGRESS_TOPIC", "github/moda-labs")
    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\nentry_point: director\n"
        "subscribe:\n  - ${INGRESS_TOPIC}\n"
    )
    assert explicit_subscriptions(bobi_install.repo_path) == ["github/moda-labs"]


def test_the_shared_parser_does_not_swallow_a_malformed_agent_yaml(bobi_install):
    """The shared parser stays non-swallowing.

    Ingress has never hidden a broken agent.yaml behind an empty subscription
    list, so folding discovery's `except Exception` INTO the parser would have
    silently changed ingress from "raise" to "no topics configured".
    """
    import yaml

    from bobi.events.subscriptions import explicit_subscriptions

    paths.agent_yaml_path(bobi_install.repo_path).write_text("subscribe: [a\n")
    with pytest.raises(yaml.YAMLError):
        explicit_subscriptions(bobi_install.repo_path)


def test_discovery_keeps_its_own_fall_through_on_a_parser_error(
        bobi_install, monkeypatch):
    """...and discovery's `except Exception` still sits at the CALL site.

    Deleting that try/except would turn a parse failure into a hard error for
    the session instead of a fall-through to auto-detection.
    """
    from bobi.events import subscriptions

    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\nentry_point: director\n"
    )

    def _boom(_project_path):
        raise ValueError("parser blew up")

    monkeypatch.setattr(subscriptions, "explicit_subscriptions", _boom)
    assert subscriptions.discover_subscriptions(bobi_install.repo_path) == [
        bobi_install.repo_path.name]
