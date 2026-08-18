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


# --- Brain CLI gating (D020) ---


def _install_brain(tmp_path, body: str) -> None:
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text("agent: t\n" + body)


class TestCheckBrainCLI:
    """The team's ENGINE decides which CLI doctor may demand.

    A codex team on a host with no `claude` binary is healthy, and reporting
    it broken (doctor exit 1) is the whole defect. Gateway mode is an
    endpoint property, not an engine, so a `kind: claude` gateway team still
    needs the claude CLI — it is the CLI that dials the gateway.
    """

    def _names(self, results):
        return {r.name for r in results}

    @pytest.fixture(autouse=True)
    def _no_live_auth_probe(self):
        """`_check_claude_auth` shells out to `claude --print hi` — a real
        model call, never from a unit test."""
        from bobi import doctor
        with patch.object(doctor, "_check_claude_auth",
                          return_value=CheckResult("Claude auth", ok=True)):
            yield

    def test_claude_team_checks_the_claude_cli(self, tmp_path):
        _install_brain(tmp_path, "brain:\n  kind: claude\n")
        from bobi import doctor
        with patch.object(doctor, "bound_root", return_value=tmp_path):
            results = doctor._check_brain_cli()
        assert self._names(results) == {"Claude CLI", "Claude auth"}

    def test_unconfigured_team_defaults_to_claude(self, tmp_path):
        _install_brain(tmp_path, "entry_point: manager\n")
        from bobi import doctor
        with patch.object(doctor, "bound_root", return_value=tmp_path):
            results = doctor._check_brain_cli()
        assert self._names(results) == {"Claude CLI", "Claude auth"}

    def test_codex_team_never_demands_the_claude_cli(self, tmp_path):
        _install_brain(tmp_path, "brain:\n  kind: codex\n")
        from bobi import doctor
        with patch("shutil.which", return_value=None), \
             patch.object(doctor, "bound_root", return_value=tmp_path):
            results = doctor._check_brain_cli()
        assert "Claude CLI" not in self._names(results)
        assert "Claude auth" not in self._names(results)
        # It still says something: the codex CLI is this team's real
        # prerequisite, and it is missing here.
        codex = [r for r in results if r.name == "Codex CLI"]
        assert codex and not codex[0].ok

    def test_codex_team_passes_when_the_codex_cli_is_present(self, tmp_path):
        _install_brain(tmp_path, "brain:\n  kind: codex\n")
        from bobi import doctor
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch.object(doctor, "bound_root", return_value=tmp_path):
            results = doctor._check_brain_cli()
        assert all(r.ok for r in results)

    def test_openai_gateway_team_is_a_codex_engine(self, tmp_path):
        _install_brain(
            tmp_path,
            "brain:\n  kind: codex\n  base_url: http://localhost:4000\n")
        from bobi import doctor
        with patch("shutil.which", return_value=None), \
             patch.object(doctor, "bound_root", return_value=tmp_path):
            results = doctor._check_brain_cli()
        assert "Claude CLI" not in self._names(results)

    def test_claude_gateway_team_needs_the_cli_but_not_vendor_auth(self, tmp_path):
        _install_brain(
            tmp_path,
            "brain:\n  kind: claude\n  base_url: http://localhost:4000\n")
        from bobi import doctor
        with patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch.object(doctor, "bound_root", return_value=tmp_path), \
             patch.object(
                 doctor, "_check_claude_auth",
                 side_effect=AssertionError(
                     "a gateway team's auth belongs to the gateway, and the "
                     "ambient `claude --print hi` probe does not carry it")):
            results = doctor._check_brain_cli()
        assert "Claude CLI" in self._names(results)
        assert all(r.ok for r in results)

    def test_no_bound_runtime_keeps_the_default_checks(self):
        from bobi import doctor
        with patch.object(doctor, "bound_root", return_value=None), \
             patch("shutil.which", return_value="/usr/local/bin/claude"), \
             patch.object(doctor, "_check_claude_auth",
                          return_value=CheckResult("Claude auth", ok=True)):
            results = doctor._check_brain_cli()
        assert self._names(results) == {"Claude CLI", "Claude auth"}


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
            from bobi.doctor import _check_long_term_memory
            r = _check_long_term_memory()
        assert r.ok
        assert "no long_term_memory.md yet" in r.detail

    def test_missing_policy_with_large_backlog_fails(self, tmp_path):
        rows = [{"id": i} for i in range(101)]
        with (
            patch("bobi.doctor.bound_root", return_value=tmp_path),
            patch("bobi.history.messages_since", return_value=rows),
        ):
            from bobi.doctor import _check_long_term_memory
            r = _check_long_term_memory()
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
        # The hint names the authored spelling (`event_server:`), not the
        # parses-but-unused `event_server_url:` alias — bobi/ingress.py (Q109).
        assert "Set event_server in agent.yaml" in r.hint
        assert "event_server_url" not in r.hint

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
            "bobi.slack.resolve_auth_info",
            return_value=("T_TEAM", "B_BOT", "U_BOT"),
        ),
        patch("bobi.slack.resolve_app_id", return_value="A_APP"),
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

    def test_reports_users_read_when_app_identity_is_missing(self, tmp_path):
        _write_slack_socket_config(tmp_path)
        import bobi.slack
        with patch(
            "bobi.slack.require_app_identity",
            side_effect=bobi.slack.SlackAppIdentityError("users:read is required"),
        ):
            result = _run_slack_socket_check(tmp_path, {
                "status": "ok", "mode": "local", "slack_socket": [],
            })
        assert not result.ok
        assert "users:read" in result.hint

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
    list_checks = {"_check_package_requires", "_check_host_caps",
                   "_check_services", "_check_brain_cli"}
    for name in (
        "_check_brain_cli",
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
        "_check_running_code",
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
    # The stale-process check is only useful if `doctor` actually runs it (#928).
    running_code = CheckResult("Running code", ok=True, detail="sentinel")
    monkeypatch.setattr(doctor, "_check_running_code", lambda: running_code)

    results = doctor.run_doctor()

    assert socket_check in results
    assert running_code in results


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


def test_event_server_check_skips_local_node_for_unregistered_remote(
    tmp_path, monkeypatch,
):
    from bobi import doctor

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: test\n"
        "event_server_url: https://events.example.com\n"
    )
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr("bobi.events.server.health", lambda url: None)
    monkeypatch.setattr(
        "bobi.events.server.resolve_node_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("remote configuration must not require local Node")
        ),
    )

    result = doctor._check_event_server()

    assert result.ok is False
    assert "Node.js" not in result.hint


# --- Event server URL resolution (D019) ---


def _install_event_server(tmp_path, url: str = "") -> None:
    paths.package_dir(tmp_path).mkdir(parents=True)
    body = "agent: test\n"
    if url:
        body += f"event_server_url: {url}\n"
    paths.agent_yaml_path(tmp_path).write_text(body)


def test_event_server_check_probes_the_configured_remote_url(
    tmp_path, monkeypatch,
):
    """D019 — a remote-configured instance that has not registered yet.

    Falling through to a hardcoded http://localhost:8080 reported the remote
    server 'not running' (a REQUIRED failure, doctor exit 1) and told the
    operator to start a local one that was never meant to run.
    """
    from bobi import doctor

    _install_event_server(tmp_path, "https://events.example.com")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    probed = []

    def fake_health(url, *a, **kw):
        probed.append(url)
        return {"status": "ok"}

    monkeypatch.setattr("bobi.events.server.health", fake_health)

    result = doctor._check_event_server()

    assert probed == ["https://events.example.com"]
    assert result.ok is True
    assert "events.example.com" in result.detail


def test_event_server_check_names_the_remote_url_when_unreachable(
    tmp_path, monkeypatch,
):
    from bobi import doctor

    _install_event_server(tmp_path, "https://events.example.com")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    monkeypatch.setattr("bobi.events.server.health", lambda url, *a, **kw: None)

    result = doctor._check_event_server()

    assert result.ok is False
    assert "events.example.com" in result.detail
    # Never the local-start advice: there is no local server to start.
    assert "event-server start" not in result.hint


def test_event_server_check_uses_the_running_local_port(tmp_path, monkeypatch):
    """A local server on a configured non-8080 port is not 'not running'."""
    from bobi import doctor

    _install_event_server(tmp_path)
    state = paths.state_dir(tmp_path)
    (state / "event-server.pid").write_text("4242")
    (state / "event-server.port").write_text("9090")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    probed = []

    def fake_health(url, *a, **kw):
        probed.append(url)
        return {"status": "ok"} if url.endswith(":9090") else None

    monkeypatch.setattr("bobi.events.server.health", fake_health)

    result = doctor._check_event_server()

    assert probed == ["http://localhost:9090"]
    assert result.ok is True


def test_event_server_check_uses_the_configured_local_port(tmp_path, monkeypatch):
    from bobi import doctor

    _install_event_server(tmp_path, "http://localhost:9091")
    monkeypatch.setattr(doctor, "bound_root", lambda: tmp_path)
    probed = []

    def fake_health(url, *a, **kw):
        probed.append(url)
        return {"status": "ok"}

    monkeypatch.setattr("bobi.events.server.health", fake_health)

    result = doctor._check_event_server()

    assert probed == ["http://localhost:9091"]
    assert result.ok is True


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
    assert result.detail == "not running"
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


# --- Running code vs installed code (#928) ---


class TestCheckRunningCode:
    """`doctor` reported a six-day-old event server healthy while the bundle
    it executes had been overwritten by an upgrade. The pack-drift check has
    always had this instinct for FILES; this is the same question asked of
    running PROCESSES."""

    @pytest.fixture
    def live_pid(self):
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            yield proc.pid
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def _bind(self, tmp_path, monkeypatch):
        paths.state_path(tmp_path).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("bobi.doctor.bound_root", lambda: tmp_path)

    def test_nothing_running_passes(self, tmp_path, monkeypatch):
        from bobi.doctor import _check_running_code

        self._bind(tmp_path, monkeypatch)

        result = _check_running_code()

        assert result.ok
        assert "no long-lived local processes" in result.detail

    def test_matching_launch_passes_and_names_the_version(self, tmp_path,
                                                          monkeypatch, live_pid):
        from bobi import launch_stamp
        from bobi.doctor import _check_running_code

        self._bind(tmp_path, monkeypatch)
        paths.manager_pid_path(tmp_path).write_text(str(live_pid))
        launch_stamp.record_launch(tmp_path, launch_stamp.MANAGER, live_pid)

        result = _check_running_code()

        assert result.ok
        assert "manager" in result.detail
        assert launch_stamp.installed_bobi_version() in result.detail

    def test_stale_processes_are_named_with_their_remedy(self, tmp_path,
                                                        monkeypatch, live_pid):
        """The upgrade case: both long-lived processes predate the install."""
        from bobi.doctor import _check_running_code

        self._bind(tmp_path, monkeypatch)
        paths.manager_pid_path(tmp_path).write_text(str(live_pid))
        paths.event_server_pid_path(tmp_path).write_text(str(live_pid))

        result = _check_running_code()

        assert not result.ok
        # A warning, not a failure: the install is fine, a restart is pending.
        assert not result.required
        assert "manager" in result.detail and "event server" in result.detail
        assert "restart" in result.hint
        assert "event-server restart" in result.hint

    def test_restarting_the_named_process_clears_the_check(self, tmp_path,
                                                           monkeypatch, live_pid):
        from bobi import launch_stamp
        from bobi.doctor import _check_running_code

        self._bind(tmp_path, monkeypatch)
        paths.event_server_pid_path(tmp_path).write_text(str(live_pid))
        assert not _check_running_code().ok

        launch_stamp.record_launch(tmp_path, launch_stamp.EVENT_SERVER, live_pid)

        assert _check_running_code().ok

    def test_no_runtime_selected_says_so(self, monkeypatch):
        from bobi.doctor import _check_running_code

        monkeypatch.setattr("bobi.doctor.bound_root", lambda: None)

        result = _check_running_code()

        assert result.ok
        assert result.detail == "no runtime selected"
