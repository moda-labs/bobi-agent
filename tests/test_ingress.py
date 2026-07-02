"""Ingress reachability diagnostics."""

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
