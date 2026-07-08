"""Tests for named doctor health checks."""

from pathlib import Path
from unittest.mock import patch

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
        assert "no policy.md yet" in r.detail

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
        assert "policy-curator appears stalled" in r.hint


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
