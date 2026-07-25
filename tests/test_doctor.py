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


# --- Brain CLI + auth (follows the team's configured brain) ---


def _install_team(tmp_path, agent_yaml: str) -> None:
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(agent_yaml)


def _on_path(*binaries: str):
    """A shutil.which stub where only *binaries* are installed."""
    return lambda binary: f"/usr/local/bin/{binary}" if binary in binaries else None


def _never_shells_out(*args, **kwargs):
    """A subprocess.run stub for teams whose brain must not be probed."""
    raise AssertionError(f"doctor must not run {args and args[0]}")


class TestCheckBrain:
    """doctor probes the brain the team actually runs (#485 brains).

    It used to run the Claude CLI + Claude auth checks unconditionally, so a
    codex or gateway team on a host with no claude binary reported a broken
    runtime (exit 1) for a CLI it never invokes.
    """

    def test_claude_team_checks_cli_and_auth(self, tmp_path, monkeypatch):
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\n")
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path("claude"))
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: Mock(returncode=0, stderr=""))

        results = doctor._check_brain()

        assert [(r.name, r.ok) for r in results] == [
            ("Claude CLI", True), ("Claude auth", True)]

    def test_codex_team_does_not_require_the_claude_cli(self, tmp_path,
                                                        monkeypatch):
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\nbrain:\n  kind: codex\n")
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path("codex"))
        monkeypatch.setattr("subprocess.run", _never_shells_out)

        results = doctor._check_brain()

        assert [r.name for r in results if not r.ok and r.required] == []
        assert [(r.name, r.ok) for r in results] == [("Codex CLI", True)]

    def test_codex_team_requires_its_own_cli(self, tmp_path, monkeypatch):
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\nbrain:\n  kind: codex\n")
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path())
        monkeypatch.setattr("subprocess.run", _never_shells_out)

        results = doctor._check_brain()

        assert [(r.name, r.ok, r.required) for r in results] == [
            ("Codex CLI", False, True)]
        assert "codex" in results[0].hint

    def test_gateway_team_skips_the_vendor_auth_probe(self, tmp_path,
                                                      monkeypatch):
        # A gateway team authenticates to its own endpoint at session launch;
        # probing vendor auth on this host fails for the wrong reason.
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\nbrain:\n"
                                "  kind: claude\n  base_url: http://gw.test\n")
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path("claude"))
        monkeypatch.setattr("subprocess.run", _never_shells_out)

        results = doctor._check_brain()

        assert [(r.name, r.ok) for r in results] == [("Claude CLI", True)]

    def test_unknown_brain_kind_is_reported_not_assumed_healthy(
        self, tmp_path, monkeypatch,
    ):
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\nbrain:\n  kind: gemini\n")
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path("claude", "codex"))
        monkeypatch.setattr("subprocess.run", _never_shells_out)

        results = doctor._check_brain()

        assert [(r.name, r.ok, r.required) for r in results] == [
            ("Brain", False, True)]
        assert "gemini" in results[0].detail

    def test_run_doctor_reports_the_configured_brain(self, tmp_path,
                                                     monkeypatch):
        import bobi.doctor as doctor

        _install_team(tmp_path, "agent: t\nbrain:\n  kind: codex\n")
        _stub_non_brain_checks(monkeypatch)
        monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
        monkeypatch.setattr(doctor.shutil, "which", _on_path("codex"))
        monkeypatch.setattr("subprocess.run", _never_shells_out)

        results = doctor.run_doctor()

        assert [r.name for r in results if not r.ok and r.required] == []
        assert any(r.name == "Codex CLI" and r.ok for r in results)


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


# Every check run_doctor runs except the brain ones, so a test can exercise the
# real run_doctor without touching the host or the network. The list checks
# return a list; the rest return one CheckResult.
_NON_BRAIN_CHECKS = (
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
)
_LIST_CHECKS = {"_check_package_requires", "_check_host_caps", "_check_services"}


def _stub_non_brain_checks(monkeypatch):
    import bobi.doctor as doctor

    ordinary = CheckResult("ordinary", ok=True)
    for name in _NON_BRAIN_CHECKS:
        result = [] if name in _LIST_CHECKS else ordinary
        monkeypatch.setattr(doctor, name, lambda result=result: result)
    monkeypatch.setattr(doctor, "_check_slack_socket_mode", lambda: None)


def test_run_doctor_surfaces_slack_socket_mode_check(monkeypatch):
    import bobi.doctor as doctor

    _stub_non_brain_checks(monkeypatch)
    monkeypatch.setattr(doctor, "_check_brain", lambda: [])

    socket_check = CheckResult(
        "Slack Socket Mode", ok=True, detail="A_APP connected", required=False,
    )
    monkeypatch.setattr(
        doctor, "_check_slack_socket_mode", lambda: socket_check, raising=False,
    )

    assert socket_check in doctor.run_doctor()


def test_event_server_check_surfaces_node_prerequisite(monkeypatch):
    from bobi import doctor
    from bobi.events.server import NodeRuntimePrerequisiteError

    monkeypatch.setattr(doctor, "bound_root", lambda: None)
    monkeypatch.setattr("bobi.events.server.health", lambda url: None)
    monkeypatch.setattr(
        "bobi.events.server.resolve_node_runtime",
        lambda: (_ for _ in ()).throw(
            NodeRuntimePrerequisiteError(
                "The local event server requires Node.js 20+, but node is missing."
            )
        ),
    )

    result = doctor._check_event_server()

    assert result.ok is False
    assert "Node.js 20+" in result.detail
    assert "Install Node.js 20+" in result.hint


def test_event_server_check_reports_healthy_remote_before_registration(
    tmp_path, monkeypatch,
):
    """A remote event_server_url is remote whether or not this runtime has
    registered a deployment yet. doctor used to probe localhost:8080 for it and
    report a REQUIRED 'not running' failure with a start-the-local-server hint.
    """
    from bobi import doctor

    _install_team(tmp_path, "agent: test\n"
                            "event_server_url: https://events.example.com\n")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.events.server.health",
        lambda url, **kw: {"status": "ok"}
        if url == "https://events.example.com" else None,
    )
    monkeypatch.setattr(
        "bobi.events.server.resolve_node_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("remote configuration must not require local Node")
        ),
    )

    result = doctor._check_event_server()

    assert result.ok is True
    assert "https://events.example.com" in result.detail


def test_event_server_check_warns_for_unreachable_remote(tmp_path, monkeypatch):
    # An unreachable remote is worth reporting, but it is not this host's
    # local server failing to start — never a required failure with a
    # `event-server start` hint.
    from bobi import doctor

    _install_team(tmp_path, "agent: test\n"
                            "event_server_url: https://events.example.com\n")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr("bobi.events.server.health", lambda url, **kw: None)
    monkeypatch.setattr(
        "bobi.events.server.resolve_node_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("remote configuration must not require local Node")
        ),
    )

    result = doctor._check_event_server()

    assert result.ok is False
    assert result.required is False
    assert "https://events.example.com" in result.detail
    assert "Node.js" not in result.hint


def test_event_server_check_probes_the_configured_local_port(
    tmp_path, monkeypatch,
):
    """A local server on a configured non-default port is running, and doctor
    must find it there — `bobi agent <n> event-server status` already does."""
    from bobi import doctor

    _install_team(tmp_path, "agent: test\n"
                            "event_server_url: http://localhost:9123\n")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.events.server.health",
        lambda url, **kw: {"status": "ok"}
        if url == "http://localhost:9123" else None,
    )

    result = doctor._check_event_server()

    assert result.ok is True
    assert "9123" in result.detail


def test_event_server_check_names_the_local_port_it_probed(tmp_path, monkeypatch):
    # The failure has to say WHERE it looked, or a non-default port reads as
    # "doctor is wrong" rather than "the server is down".
    from bobi import doctor

    _install_team(tmp_path, "agent: test\n"
                            "event_server_url: http://localhost:9123\n")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr("bobi.events.server.health", lambda url, **kw: None)
    monkeypatch.setattr("bobi.events.server.resolve_node_runtime",
                        lambda: ("/usr/bin/node", "v20.19.2"))

    result = doctor._check_event_server()

    assert result.ok is False
    assert result.detail == "not running (http://localhost:9123)"


def test_event_server_check_keeps_normal_not_running_hint_when_node_is_ready(
    monkeypatch,
):
    from bobi import doctor

    monkeypatch.setattr(doctor, "bound_root", lambda: None)
    monkeypatch.setattr("bobi.events.server.health", lambda url: None)
    monkeypatch.setattr(
        "bobi.events.server.resolve_node_runtime",
        lambda: ("/usr/bin/node", "v20.19.2"),
    )

    result = doctor._check_event_server()

    assert result.ok is False
    assert result.detail == "not running (http://localhost:8080)"
    assert "auto-launch" in result.hint


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
