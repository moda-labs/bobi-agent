"""CLI contract tests for the Bobi Agent command tree."""

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bobi.__version__ import __version__
from bobi import paths
from bobi.cli import main
from bobi.subagent import CheckResult
from tests.conftest import TEST_AGENT_NAME


def test_version_flag():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "bobi" in result.output
    assert __version__ in result.output


def test_top_level_help_is_machine_scoped():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "agent" in result.output
    assert "agents" in result.output
    assert "setup" in result.output
    for removed in [" start", " stop", " status", " workflows", " monitors"]:
        assert removed not in result.output


def test_agents_help_lists_machine_commands():
    result = CliRunner().invoke(main, ["agents", "--help"])
    assert result.exit_code == 0
    assert "setup" not in result.output
    for cmd in ["install", "list", "browse", "add-registry"]:
        assert cmd in result.output


def test_agent_help_lists_runtime_commands(bobi_install):
    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "--help"])
    assert result.exit_code == 0, result.output
    for cmd in ["start", "stop", "status", "workflows", "monitors",
                "subagents", "event-server", "login-bootstrap"]:
        assert cmd in result.output


def test_missing_agent_errors_without_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(main, ["agent", "missing", "status"])
    assert result.exit_code != 0
    assert "Bobi Agent 'missing' is not installed" in result.output
    assert "package/agent.yaml" in result.output


def test_agent_ui_app_does_not_require_local_install(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("bobi.agentui.remote.run", fake_run)
    result = CliRunner().invoke(
        main, ["agent", "canary", "ui", "--app", "ci-canary", "--check"])

    assert result.exit_code == 0, result.output
    assert calls == [{
        "name": None,
        "app": "ci-canary",
        "local_port": None,
        "remote_port": None,
        "open_browser": True,
        "check": True,
    }]


def test_agents_list_without_installs_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(main, ["agents", "list"])
    assert result.exit_code == 0
    assert "No Bobi Agents installed" in result.output


def test_agents_list_shows_installed_agent(bobi_install):
    result = CliRunner().invoke(main, ["agents", "list"])
    assert result.exit_code == 0, result.output
    assert TEST_AGENT_NAME in result.output
    assert str(bobi_install.repo_path) in result.output


def test_workflow_list_shows_installed_workflows(bobi_install):
    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "workflows", "list"])
    assert result.exit_code == 0, result.output
    assert "adhoc" in result.output


def test_workflow_validate_is_agent_scoped(bobi_install, tmp_path):
    wf_file = tmp_path / "test.yaml"
    wf_file.write_text(
        "name: test-wf\ntrigger: manual\nsteps:\n"
        "  - name: s1\n    type: prompt\n    prompt: hello\n"
    )
    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "workflows", "validate", str(wf_file)])
    assert result.exit_code == 0, result.output
    assert "Valid" in result.output


class TestSubagents:
    def test_launch_adhoc_workflow(self, bobi_install):
        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-42") as mock:
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer", "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-adhoc-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"
        assert mock.call_args[1]["role"] == "engineer"
        assert mock.call_args[1]["cwd"] == str(bobi_install.repo_path)

    def test_workflow_required(self, bobi_install):
        result = CliRunner().invoke(main, [
            "agent", TEST_AGENT_NAME, "subagents", "launch",
            "--role", "engineer", "--task", "X",
        ])
        assert result.exit_code != 0
        assert "--workflow" in result.output

    def test_role_required(self, bobi_install):
        result = CliRunner().invoke(main, [
            "agent", TEST_AGENT_NAME, "subagents", "launch",
            "-w", "adhoc", "--task", "X",
        ])
        assert result.exit_code != 0
        assert "--role" in result.output

    def test_invalid_role(self, bobi_install):
        result = CliRunner().invoke(main, [
            "agent", TEST_AGENT_NAME, "subagents", "launch",
            "-w", "adhoc", "--role", "nonexistent", "--task", "X",
        ])
        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_wait_mode_runs_check(self, bobi_install):
        check = CheckResult(success=True, finding=False)
        with patch("bobi.subagent.run_check_blocking", return_value=check):
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer",
                "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0, result.output

    def test_passes_requested_by(self, bobi_install):
        req = '{"requester":"Alice","source":{"kind":"test"},"ids":[1,2]}'
        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-1") as mock:
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "subagents", "launch",
                "-w", "adhoc", "--role", "engineer",
                "--task", "Fix #1", "--requested-by", req,
            ])
        assert result.exit_code == 0, result.output
        assert mock.call_args[1]["task"] == "Fix #1"
        assert mock.call_args[1]["requested_by"] == {
            "requester": "Alice",
            "source": {"kind": "test"},
            "ids": [1, 2],
        }


class TestEventsCommand:
    def _run_events(self, bobi_install):
        return CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "events"])

    def test_skips_malformed_lines_in_events_jsonl(self, bobi_install):
        good = {"timestamp": "2026-01-01T00:00:00", "source": "github",
                "type": "push", "data": {}}
        (bobi_install.state_dir / "events-default.jsonl").write_text(
            json.dumps(good) + "\n"
            + "NOT VALID JSON\n"
            + json.dumps({**good, "type": "pr"}) + "\n"
        )
        result = self._run_events(bobi_install)
        assert result.exit_code == 0, result.output
        assert "push" in result.output
        assert "pr" in result.output
        assert "1 malformed" in result.output

    def test_skips_malformed_lines_in_decisions_jsonl(self, bobi_install):
        good = {"timestamp": "2026-01-01T00:00:00",
                "actions": [{"type": "deploy"}], "reasoning": "ship it"}
        (bobi_install.state_dir / "decisions.jsonl").write_text(
            json.dumps(good) + "\nCORRUPTED\n"
        )
        result = self._run_events(bobi_install)
        assert result.exit_code == 0, result.output
        assert "deploy" in result.output
        assert "1 malformed" in result.output

    def test_deduplicates_events_by_seq_deployment(self, bobi_install):
        ev = {"timestamp": "2026-01-01T00:00:01", "source": "github",
              "type": "push", "seq": 5, "deployment_id": "d1"}
        (bobi_install.state_dir / "events-sess-a.jsonl").write_text(json.dumps(ev) + "\n")
        (bobi_install.state_dir / "events-sess-b.jsonl").write_text(json.dumps(ev) + "\n")
        result = self._run_events(bobi_install)
        assert result.exit_code == 0, result.output
        assert result.output.count("push") == 1

    def test_payload_event_renders_text(self, bobi_install):
        ev = {
            "timestamp": "2026-01-01T00:00:01",
            "source": "inbox",
            "type": "message",
            "payload": {"sender": "alice", "text": "hello world"},
        }
        (bobi_install.state_dir / "events-sess-a.jsonl").write_text(json.dumps(ev) + "\n")
        result = self._run_events(bobi_install)
        assert result.exit_code == 0, result.output
        assert "alice" in result.output
        assert "hello world" in result.output

    def test_ignores_legacy_events_jsonl(self, bobi_install):
        legacy = {"timestamp": "2026-01-01T00:00:01", "source": "github",
                  "type": "legacy_push"}
        session = {"timestamp": "2026-01-01T00:00:02", "source": "github",
                   "type": "new_pr", "seq": 1, "deployment_id": "d1"}
        (bobi_install.state_dir / "events.jsonl").write_text(json.dumps(legacy) + "\n")
        (bobi_install.state_dir / "events-sess-a.jsonl").write_text(json.dumps(session) + "\n")
        result = self._run_events(bobi_install)
        assert result.exit_code == 0, result.output
        assert "legacy_push" not in result.output
        assert "new_pr" in result.output


class TestEventServerCommand:
    def test_status_uses_selected_runtime_port_file(self, bobi_install, monkeypatch):
        (bobi_install.state_dir / "event-server.pid").write_text("12345")
        (bobi_install.state_dir / "event-server.port").write_text("58405")

        seen = []

        def fake_health(url):
            seen.append(url)
            if url == "http://localhost:58405":
                return {"status": "ok", "mode": "local", "deployments": 2}
            return None

        monkeypatch.setattr("bobi.events.server.health", fake_health)

        result = CliRunner().invoke(
            main, ["agent", TEST_AGENT_NAME, "event-server", "status"])

        assert result.exit_code == 0, result.output
        assert "running on port 58405" in result.output
        assert seen == ["http://localhost:58405"]

    def test_start_uses_configured_local_event_server_port(self, bobi_install, monkeypatch):
        paths.agent_yaml_path(bobi_install.repo_path).write_text(
            "agent: test-agent\n"
            "entry_point: director\n"
            "event_server: http://localhost:17777\n"
        )
        called = {}

        def fake_ensure_running(port, project_path=None):
            called["port"] = port
            called["project_path"] = project_path
            return "connected"

        monkeypatch.setattr("bobi.events.server.ensure_running", fake_ensure_running)

        result = CliRunner().invoke(
            main, ["agent", TEST_AGENT_NAME, "event-server", "start"])

        assert result.exit_code == 0, result.output
        assert called == {"port": 17777, "project_path": bobi_install.repo_path}
        assert "port 17777" in result.output

    def test_stop_warning_uses_selected_runtime_port(self, bobi_install, monkeypatch):
        (bobi_install.state_dir / "event-server.port").write_text("58405")

        def fake_health(url):
            assert url == "http://localhost:58405"
            return {"status": "ok", "mode": "local", "deployments": 1}

        monkeypatch.setattr("bobi.events.server.health", fake_health)

        result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "stop"])

        assert result.exit_code == 0, result.output
        assert "Event server is still running on port 58405" in result.output


class TestSetupCommand:
    def _home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        return home

    def test_missing_claude_cli_fails_with_hint(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("bobi.sdk.get_cli_path", lambda: "/nonexistent/claude")
        result = CliRunner().invoke(main, ["setup", "alpha"])
        assert result.exit_code != 0
        assert "Claude Code CLI" in result.output

    def test_help(self):
        result = CliRunner().invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "--resume" in result.output

    def test_runs_setup_against_agent_run_root(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        seen = {}

        def fake_run_setup(project_path, model=None, resume=False):
            seen.update(project=project_path, model=model, resume=resume)
            return 0

        monkeypatch.setattr("bobi.setup.run_setup", fake_run_setup)
        result = CliRunner().invoke(
            main, ["setup", "alpha", "--model", "sonnet"])
        assert result.exit_code == 0, result.output
        assert seen["project"] == paths.agent_run_root("alpha")
        assert seen["model"] == "sonnet"
        assert seen["resume"] is False

    def test_interrupted_setup_requires_confirmation(self, tmp_path, monkeypatch):
        from bobi.setup.state import SetupState, Stage

        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        called = {}
        monkeypatch.setattr("bobi.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        project = paths.agent_run_root("alpha")
        paths.state_dir(project)
        SetupState(stage=Stage.DESIGN, team_name="alpha").save(project)

        declined = CliRunner().invoke(main, ["setup", "alpha"], input="n\n")
        assert declined.exit_code != 0
        assert "--resume" in declined.output
        assert "ran" not in called

    def test_existing_install_requires_confirmation(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        called = {}
        monkeypatch.setattr("bobi.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        project = paths.agent_run_root("alpha")
        package = paths.package_dir(project)
        package.mkdir(parents=True)
        (package / "agent.yaml").write_text("agent: alpha\n")

        declined = CliRunner().invoke(main, ["setup", "alpha"], input="n\n")
        assert declined.exit_code != 0
        assert "ran" not in called
        accepted = CliRunner().invoke(main, ["setup", "alpha"], input="y\n")
        assert accepted.exit_code == 0, accepted.output
        assert called.get("ran") is True

    def test_resume_skips_confirmation(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("bobi.setup.run_setup", lambda *a, **k: 0)
        result = CliRunner().invoke(main, ["setup", "alpha", "--resume"])
        assert result.exit_code == 0, result.output


class TestMonitorAdd:
    def _add(self, bobi_install, args):
        return CliRunner().invoke(
            main, ["agent", TEST_AGENT_NAME, "monitors", "add", *args])

    def _written(self, bobi_install):
        import yaml
        path = paths.package_dir(bobi_install.repo_path) / "monitors.yaml"
        return yaml.safe_load(path.read_text())["monitors"]

    def test_interval_monitor_still_works(self, bobi_install):
        result = self._add(bobi_install, [
            "pr check", "--interval", "15m", "--description", "check PRs"])
        assert result.exit_code == 0, result.output
        rec = self._written(bobi_install)[0]
        assert rec["name"] == "pr-check"
        assert rec["interval"] == "15m"
        assert "at" not in rec

    def test_weekly_notify_monitor_writes_at_days_tz(self, bobi_install):
        result = self._add(bobi_install, [
            "weekly-prep-doc", "--at", "21:00", "--days", "sun",
            "--tz", "America/Los_Angeles", "--notify",
            "--event", "monitor/prep.weekly_due",
            "--description", "Generate my prep doc for the upcoming week",
        ])
        assert result.exit_code == 0, result.output
        rec = self._written(bobi_install)[0]
        assert rec["name"] == "weekly-prep-doc"
        assert rec["at"] == ["21:00"]
        assert rec["days"] == ["sun"]
        assert rec["tz"] == "America/Los_Angeles"
        assert rec["notify"] is True
        assert rec["event"] == "monitor/prep.weekly_due"
        assert "interval" not in rec

    def test_interval_and_at_are_mutually_exclusive(self, bobi_install):
        result = self._add(bobi_install, ["x", "--interval", "5m", "--at", "21:00"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_days_without_at_is_rejected(self, bobi_install):
        result = self._add(bobi_install, ["x", "--days", "sun"])
        assert result.exit_code != 0
        assert "--days only applies to --at" in result.output

    def test_invalid_at_time_is_rejected(self, bobi_install):
        result = self._add(bobi_install, ["x", "--at", "25:00"])
        assert result.exit_code != 0
        assert "at-time" in result.output

    def test_invalid_weekday_is_rejected(self, bobi_install):
        result = self._add(bobi_install, ["x", "--at", "21:00", "--days", "funday"])
        assert result.exit_code != 0
        assert "weekday" in result.output.lower()
