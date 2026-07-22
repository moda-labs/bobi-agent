"""Tests for named doctor health checks."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bobi import paths


# --- CheckResult ---

from bobi.doctor import CheckResult


class TestCheckResult:
    def test_ok_result(self):
        r = CheckResult("Test", ok=True, detail="all good")
        assert r.ok
        assert r.detail == "all good"

    def test_failed_result(self):
        r = CheckResult("Test", ok=False, detail="missing", hint="fix it")
        assert not r.ok
        assert r.hint == "fix it"


# --- Claude CLI ---

class TestCheckCLI:
    def test_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            from bobi.doctor import _check_claude_cli
            r = _check_claude_cli()
        assert r.ok


# --- Project ---


class TestCheckProjectConfig:

    def test_passes_when_exists(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "entry_point: manager\nevent_server_url: https://events.test\n")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_local_config
            r = _check_local_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_local_config
            r = _check_local_config()
        assert not r.ok
        assert "missing" in r.detail


class TestCheckServices:
    def test_explicit_codex_chat_wire_api_is_warning(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: local-team\n"
            "brain:\n"
            "  kind: codex\n"
            "  base_url: http://localhost:9000/v1\n"
            "  wire_api: chat\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_services
            checks = _check_services()

        warning = next(c for c in checks if c.name == "brain.gateway_openai")
        assert not warning.ok
        assert not warning.required
        assert "wire_api: chat" in warning.detail
        assert "LiteLLM" in warning.hint


# --- Team policy ---

class TestCheckPolicy:
    def test_missing_policy_is_ok_for_fresh_runtime(self, tmp_path):
        with (
            patch("bobi.doctor.bound_root", return_value=tmp_path),
            patch("bobi.history.messages_since", return_value=[]),
        ):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert r.ok
        assert "no long_term_memory.md yet" in r.detail

    def test_missing_policy_with_large_backlog_fails(self, tmp_path):
        rows = [{"id": i} for i in range(101)]
        with (
            patch("bobi.doctor.bound_root", return_value=tmp_path),
            patch("bobi.history.messages_since", return_value=rows),
        ):
            from bobi.doctor import _check_policy
            r = _check_policy()
        assert not r.ok
        assert "pending" in r.detail
        assert "sleep cycle appears stalled" in r.hint


# --- Ingress reachability ---

class TestCheckIngressReachability:

    def test_warns_for_external_events_on_loopback(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: test\n"
            "services:\n"
            "  - name: slack\n"
            "    events: true\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_ingress_reachability
            r = _check_ingress_reachability()
        assert not r.ok
        assert not r.required
        assert "slack" in r.detail
        assert "public HTTPS ingress" in r.detail
        assert "event_server_url" in r.hint

    def test_passes_for_remote_event_server(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: test\n"
            "event_server_url: https://events.example.com\n"
            "services:\n"
            "  - name: slack\n"
            "    events: true\n"
        )
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_ingress_reachability
            r = _check_ingress_reachability()
        assert r.ok

    def test_malformed_config_does_not_crash_doctor_check(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("agent: [broken\n")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_ingress_reachability
            r = _check_ingress_reachability()
        assert r.ok
        assert "skipped" in r.detail


# --- Slack Socket Mode ---

def _write_slack_socket_config(tmp_path, *, event_server_url=""):
    paths.package_dir(tmp_path).mkdir(parents=True)
    lines = ["agent: test"]
    if event_server_url:
        lines.append(f"event_server_url: {event_server_url}")
    lines.extend([
        "services:",
        "  - name: slack",
        "    events: true",
        "    credentials:",
        "      bot_token: xoxb-configured",
        "      app_token: xapp-configured",
    ])
    paths.agent_yaml_path(tmp_path).write_text("\n".join(lines) + "\n")


def _run_slack_socket_check(
    tmp_path, health_payload, *, expected_url="http://localhost:8080",
):
    health_probe = Mock(return_value=health_payload)
    with (
        patch("bobi.doctor.bound_root", return_value=tmp_path),
        patch("bobi.events.server.health", health_probe),
        patch(
            "bobi.events.server._slack_auth_info",
            return_value=("T_TEAM", "B_BOT", "U_BOT"),
        ),
        patch("bobi.events.server._slack_app_id", return_value="A_APP"),
    ):
        from bobi.doctor import _check_slack_socket_mode
        result = _check_slack_socket_mode()
    health_probe.assert_called_once_with(expected_url)
    return result


class TestCheckSlackSocketMode:
    def test_flags_app_token_paired_with_remote_event_server(self, tmp_path):
        _write_slack_socket_config(
            tmp_path, event_server_url="https://events.example.com",
        )

        result = _run_slack_socket_check(
            tmp_path, {"status": "ok", "mode": "worker"},
            expected_url="https://events.example.com",
        )

        assert not result.ok
        assert not result.required
        assert result.name == "Slack Socket Mode"
        assert "remote" in (result.detail + result.hint).lower()
        assert "local" in result.hint.lower()
        assert "https://events.example.com" in result.detail
        assert "xapp-configured" not in result.detail + result.hint

    def test_flags_local_server_missing_socket_health_block(self, tmp_path):
        _write_slack_socket_config(tmp_path)

        result = _run_slack_socket_check(
            tmp_path, {"status": "ok", "mode": "local"},
        )

        assert not result.ok
        assert not result.required
        assert any(
            word in (result.detail + result.hint).lower()
            for word in ("unsupported", "not registered", "unavailable")
        )

    def test_flags_unavailable_health_without_exposing_token(self, tmp_path):
        _write_slack_socket_config(tmp_path)

        result = _run_slack_socket_check(tmp_path, None)

        assert not result.ok
        assert not result.required
        assert "unavailable" in (result.detail + result.hint).lower()
        assert "http://localhost:8080" in result.detail
        assert "xapp-configured" not in result.detail + result.hint

    def test_reports_matching_connected_socket(self, tmp_path):
        _write_slack_socket_config(tmp_path)
        health_payload = {
            "status": "ok",
            "mode": "local",
            "slack_socket": [{
                "application_id": "A_APP",
                "state": "connected",
                "connect_count": 2,
                "delivered_event_count": 5,
                "last_event_at": "2026-07-22T12:00:00.000Z",
            }],
        }

        result = _run_slack_socket_check(tmp_path, health_payload)

        assert result.ok
        assert not result.required
        assert "connected" in result.detail.lower()
        assert "A_APP" in result.detail

    @pytest.mark.parametrize("entry", [
        pytest.param(
            {"application_id": "A_APP", "state": "backoff"},
            id="transient-backoff",
        ),
        pytest.param(
            {
                "application_id": "A_APP",
                "state": "fatal",
                "fatal_reason": "authentication failed",
            },
            id="fatal",
        ),
    ])
    def test_reports_matching_nonconnected_socket_as_warning(
        self, tmp_path, entry,
    ):
        _write_slack_socket_config(tmp_path)

        result = _run_slack_socket_check(tmp_path, {
            "status": "ok",
            "mode": "local",
            "slack_socket": [entry],
        })

        assert not result.ok
        assert not result.required
        assert entry["state"] in (result.detail + result.hint).lower()
        if entry["state"] == "fatal":
            assert "authentication failed" in result.detail + result.hint

    def test_does_not_treat_another_apps_connection_as_healthy(self, tmp_path):
        _write_slack_socket_config(tmp_path)

        result = _run_slack_socket_check(tmp_path, {
            "status": "ok",
            "mode": "local",
            "slack_socket": [{
                "application_id": "A_OTHER",
                "state": "connected",
            }],
        })

        assert not result.ok
        assert not result.required
        assert "A_APP" in result.detail + result.hint
        assert "not registered" in (result.detail + result.hint).lower()

    def test_redacts_secret_and_control_characters_from_health(self, tmp_path):
        _write_slack_socket_config(tmp_path)

        result = _run_slack_socket_check(tmp_path, {
            "status": "ok",
            "mode": "local",
            "slack_socket": [{
                "application_id": "A_APP",
                "state": "fatal\x1b[31m",
                "fatal_reason": "token xapp-configured rejected\nretry",
            }],
        })

        output = result.detail + result.hint
        assert "xapp-configured" not in output
        assert "\x1b" not in output
        assert "\n" not in output

    def test_whitespace_app_token_is_treated_as_unconfigured(
        self, tmp_path,
    ):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: test\n"
            "services:\n"
            "  - name: slack\n"
            "    credentials:\n"
            "      bot_token: xoxb-configured\n"
            "      app_token: '   '\n"
        )
        with (
            patch("bobi.doctor.bound_root", return_value=tmp_path),
            patch(
                "bobi.events.server.health",
                side_effect=AssertionError(
                    "blank app token must not probe socket health"
                ),
            ),
        ):
            from bobi.doctor import _check_slack_socket_mode
            assert _check_slack_socket_mode() is None

    def test_is_omitted_without_app_token_and_does_not_probe_health(
        self, tmp_path,
    ):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: test\n"
            "services:\n"
            "  - name: slack\n"
            "    credentials:\n"
            "      bot_token: xoxb-configured\n"
        )
        with (
            patch("bobi.doctor.bound_root", return_value=tmp_path),
            patch(
                "bobi.events.server.health",
                side_effect=AssertionError(
                    "webhook-only Slack must not probe socket health"
                ),
            ),
        ):
            from bobi.doctor import _check_slack_socket_mode
            assert _check_slack_socket_mode() is None


def test_run_doctor_surfaces_slack_socket_mode_check(monkeypatch):
    import bobi.doctor as doctor

    ordinary = CheckResult("ordinary", ok=True)
    list_checks = {"_check_package_requires", "_check_host_caps", "_check_services"}
    for name in (
        "_check_claude_cli",
        "_check_claude_auth",
        "_check_local_config",
        "_check_runtime_layout",
        "_check_runtime_write_policy",
        "_check_install_integrity",
        "_check_bobi_install_integrity",
        "_check_package_requires",
        "_check_host_caps",
        "_check_services",
        "_check_workflows",
        "_check_bubble_auth",
        "_check_event_server",
        "_check_ingress_reachability",
        "_check_recent_events",
        "_check_long_term_memory",
    ):
        result = [] if name in list_checks else ordinary
        monkeypatch.setattr(doctor, name, lambda result=result: result)

    socket_check = CheckResult(
        "Slack Socket Mode", ok=True, detail="A_APP connected", required=False,
    )
    monkeypatch.setattr(
        doctor, "_check_slack_socket_mode", lambda: socket_check, raising=False,
    )

    assert socket_check in doctor.run_doctor()


# --- Host capabilities (#428 Stage 3) ---


class TestCheckHostCaps:
    def _install(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "agent: t\nhost:\n  - sysctl: net.example.knob=0\n")

    def test_no_host_block_no_checks(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("agent: t\n")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_host_caps
            assert _check_host_caps() == []

    def test_satisfied_cap_passes(self, tmp_path):
        self._install(tmp_path)
        knob = tmp_path / "knob"; knob.write_text("0\n")
        from bobi.host_caps import HostCap
        with patch("bobi.doctor.bound_root", return_value=tmp_path), \
             patch.object(HostCap, "proc_path", property(lambda self: knob)):
            from bobi.doctor import _check_host_caps
            results = _check_host_caps()
        assert len(results) == 1 and results[0].ok

    def test_violated_cap_fails_with_fix(self, tmp_path):
        self._install(tmp_path)
        knob = tmp_path / "knob"; knob.write_text("1\n")
        from bobi.host_caps import HostCap
        with patch("bobi.doctor.bound_root", return_value=tmp_path), \
             patch.object(HostCap, "proc_path", property(lambda self: knob)):
            from bobi.doctor import _check_host_caps
            results = _check_host_caps()
        assert len(results) == 1 and not results[0].ok
        assert "sudo sysctl -w net.example.knob=0" in results[0].hint


# --- Runtime layout ---

class TestCheckRuntimeLayout:

    def test_passes_with_canonical_runtime(self, tmp_path):
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("agent: test\n")
        paths.state_dir(tmp_path)
        paths.workspace_dir(tmp_path).mkdir()
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_runtime_layout
            r = _check_runtime_layout()
        assert r.ok
        assert str(tmp_path) in r.detail

    def test_flags_missing_package_config(self, tmp_path):
        paths.state_dir(tmp_path)
        paths.workspace_dir(tmp_path).mkdir()
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_runtime_layout
            r = _check_runtime_layout()
        assert not r.ok
        assert "package/agent.yaml" in r.detail

    def test_fails_without_bound_root(self):
        with patch("bobi.doctor.bound_root", return_value=None):
            from bobi.doctor import _check_runtime_layout
            r = _check_runtime_layout()
        assert not r.ok
        assert "no Bobi Agent runtime" in r.detail


# --- Package requires ---


class TestCheckPackageRequires:

    def _write_config(self, tmp_path, requires_yaml):
        from textwrap import dedent
        config_dir = paths.package_dir(tmp_path)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text(dedent(f"""
            entry_point: manager
            requires:
{requires_yaml}
        """))

    def test_all_pass(self, tmp_path):
        self._write_config(tmp_path, """\
              - name: good-dep
                check: "true"
                fix: "install it" """)
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert results[0].ok
        assert "good-dep" in results[0].name

    def test_check_fails(self, tmp_path):
        self._write_config(tmp_path, """\
              - name: broken-dep
                check: "false"
                why: "needed for tests"
                fix: "run setup" """)
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert not results[0].ok
        assert "run setup" in results[0].hint

    def test_no_requires(self, tmp_path):
        config_dir = paths.package_dir(tmp_path)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        with patch("bobi.doctor.bound_root", return_value=tmp_path):
            from bobi.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []

    def test_no_project_root(self):
        with patch("bobi.doctor.bound_root", return_value=None):
            from bobi.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []
