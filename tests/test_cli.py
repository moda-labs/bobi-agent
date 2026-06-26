"""Tests for CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from bobi.__version__ import __version__
from bobi.cli import main
from bobi.subagent import CheckResult
from bobi import http as pooled


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "bobi" in result.output
    assert __version__ in result.output


def _fresh_publish():
    """Clear the memoized event-server URL between tests."""
    from bobi.events import publish
    publish._es_url_cache.clear()
    return publish


def test_post_event_posts_to_event_server():
    publish = _fresh_publish()

    captured = {}

    def _handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"delivered_to": 0})

    transport = httpx.MockTransport(_handler)
    mock_client = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_client), \
         patch("bobi.config.Config.load") as mock_pc, \
         patch("bobi.config.load_bubble_state",
               return_value={"bubble_id": "bub_t", "bubble_key": "bkey_t"}):
        mock_pc.return_value = type("PC", (), {"event_server_url": "https://events.test"})()
        ok = publish.post_event("monitor/deploy.down", {"summary": "down"},
                                project_path=Path("/tmp/repo"))

    assert ok is True
    assert captured["url"] == "https://events.test/events/deploy.down"
    assert captured["body"]["source"] == "monitor"
    assert captured["body"]["payload"] == {"summary": "down"}


def test_post_event_defaults_source_when_no_slash():
    publish = _fresh_publish()

    captured = {}

    def _handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"delivered_to": 1})

    transport = httpx.MockTransport(_handler)
    mock_client = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_client), \
         patch("bobi.config.Config.load") as mock_pc, \
         patch("bobi.config.load_bubble_state",
               return_value={"bubble_id": "bub_t", "bubble_key": "bkey_t"}):
        mock_pc.return_value = type("PC", (), {"event_server_url": "http://localhost:8080"})()
        publish.post_event("deploy_down", {}, project_path=Path("/tmp/repo"))

    assert captured["body"]["source"] == "monitor"
    assert "deploy_down" in captured["url"]


def test_post_event_returns_false_on_connection_error():
    publish = _fresh_publish()

    def _raise(request):
        raise httpx.ConnectError("nope")

    transport = httpx.MockTransport(_raise)
    mock_client = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_client), \
         patch("bobi.config.Config.load") as mock_pc, \
         patch("bobi.config.load_bubble_state",
               return_value={"bubble_id": "bub_t", "bubble_key": "bkey_t"}):
        mock_pc.return_value = type("PC", (), {"event_server_url": "http://localhost:8080"})()
        assert publish.post_event("monitor/x", {}, project_path=Path("/tmp/repo")) is False


# --- bobi workflows list ------------------------------------------------


def test_workflow_list_shows_installed_workflows(bobi_install):
    """Workflows resolve only from the installed pack, not from the framework."""
    runner = CliRunner()
    with patch("bobi.cli._detect_project_root",
               return_value=bobi_install.repo_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0
    # The bobi_install fixture installs an adhoc workflow in .bobi/workflows/
    assert "adhoc" in result.output


def test_workflow_list_empty_without_pack(tmp_path):
    """Without an installed pack, no workflows should resolve."""
    runner = CliRunner()
    with patch("bobi.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0
    assert "No workflows loaded" in result.output


def test_workflow_list_no_errors(tmp_path):
    (tmp_path / ".bobi" / "workflows").mkdir(parents=True)
    (tmp_path / ".bobi" / "agent.yaml").write_text("name: t\n")
    runner = CliRunner()
    with patch("bobi.cli._detect_project_root", return_value=tmp_path):
        result = runner.invoke(main, ["workflows", "list"])
    assert result.exit_code == 0


# --- bobi agents (unified command) --------------------------------------


class TestAgentCommand:
    def test_adhoc_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-42") as mock, \
             patch("bobi.cli._detect_project_root", return_value=tmp_path), \
             patch("bobi.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--task", "Fix #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-adhoc-42" in result.output
        mock.assert_called_once()
        assert mock.call_args[1]["workflow_name"] == "adhoc"
        assert mock.call_args[1]["task"] == "Fix #42"
        assert mock.call_args[1]["role"] == "engineer"

    def test_issue_lifecycle_workflow(self, tmp_path):
        runner = CliRunner()
        with patch("bobi.subagent.launch_agent", return_value="wf-issue-lifecycle-42") as mock, \
             patch("bobi.cli._detect_project_root", return_value=tmp_path), \
             patch("bobi.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "issue-lifecycle", "--role", "engineer",
                "--task", "Work on #42",
            ])
        assert result.exit_code == 0, result.output
        assert "wf-issue-lifecycle-42" in result.output
        assert mock.call_args[1]["workflow_name"] == "issue-lifecycle"

    def test_workflow_required(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agents", "launch", "--role", "engineer", "--task", "X"])
        assert result.exit_code != 0

    def test_role_required(self, tmp_path):
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["agents", "launch", "-w", "adhoc", "--task", "X"])
        assert result.exit_code != 0
        assert "--role" in result.output

    def test_invalid_role(self, tmp_path):
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "nonexistent",
                "--task", "X",
            ])
        assert result.exit_code != 0
        assert "Unknown role" in result.output

    def test_wait_mode_runs_check(self, tmp_path):
        runner = CliRunner()
        check = CheckResult(success=True, finding=False)
        with patch("bobi.subagent.run_check_blocking", return_value=check), \
             patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--wait", "--task", "Check prod URL",
            ])
        assert result.exit_code == 0

    def test_requires_repo(self):
        import click
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root",
                   side_effect=click.UsageError("no Bobi installation found above /x")):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer", "--task", "do a thing",
            ])
        assert result.exit_code != 0
        assert "no bobi installation" in result.output.lower()

    def test_passes_requested_by(self, tmp_path):
        runner = CliRunner()
        req = '{"from":"Alice","channel":"C1"}'
        with patch("bobi.subagent.launch_agent", return_value="wf-adhoc-1") as mock, \
             patch("bobi.cli._detect_project_root", return_value=tmp_path), \
             patch("bobi.prompts.resolver.validate_role", return_value=True):
            result = runner.invoke(main, [
                "agents", "launch", "-w", "adhoc", "--role", "engineer",
                "--task", "Fix #1",
                "--requested-by", req,
            ])
        assert result.exit_code == 0
        assert mock.call_args[1]["requested_by"] == {"from": "Alice", "channel": "C1"}


# --- monitor event subscription -----------------------------------------------


class TestMonitorEventSubscription:
    """Regression for #216: coordinator must subscribe to monitor events
    even when all event services use native adapters."""

    def test_monitor_events_subscribed_from_registry(self, bobi_install):
        """Monitor events from defaults.yaml must appear in the subscribe list,
        regardless of adapter configuration.

        Subscription is now owned by the manager Session: ``_run_from_config``
        passes the discovered topics to ``spawn_adhoc(subscribe=...)``, and the
        Session subscribes to ``inbox/<self>`` plus those on start.
        """
        with patch("bobi.cli._manager_session_name", return_value="moda-director-repo"), \
             patch("bobi.monitors.scheduler.MonitorScheduler"), \
             patch("bobi.prompts.resolver.build_startup_prompt", return_value="go"), \
             patch("bobi.subagent.spawn_adhoc") as mock_spawn, \
             patch("bobi.events.subscriptions.discover_subscriptions",
                   return_value=["github:o/r"]):
            from bobi.config import Config
            cfg = Config.load(bobi_install.repo_path)
            from bobi.cli import _run_from_config
            _run_from_config(bobi_install.repo_path, cfg)

        mock_spawn.assert_called_once()
        subscribe = mock_spawn.call_args[1]["subscribe"]
        assert "monitor/test" in subscribe, (
            "Monitor event topic from defaults.yaml was not subscribed"
        )


# --- bobi events (malformed line handling) --------------------------------


class TestEventsCommand:
    """The events command must not crash on malformed JSONL lines."""

    def test_skips_malformed_lines_in_events_jsonl(self, tmp_path):
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        good = {"timestamp": "2026-01-01T00:00:00", "source": "github", "type": "push", "data": {}}
        # Write a good line, a corrupted line, and another good line
        (state / "events-default.jsonl").write_text(
            json.dumps(good) + "\n"
            + "NOT VALID JSON\n"
            + json.dumps({**good, "type": "pr"}) + "\n"
        )
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "push" in result.output
        assert "pr" in result.output
        assert "1 malformed" in result.output

    def test_skips_malformed_lines_in_decisions_jsonl(self, tmp_path):
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        good = {"timestamp": "2026-01-01T00:00:00", "actions": [{"type": "deploy"}], "reasoning": "ship it"}
        (state / "decisions.jsonl").write_text(
            json.dumps(good) + "\n"
            + "CORRUPTED\n"
        )
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "deploy" in result.output
        assert "1 malformed" in result.output

    def test_reads_per_session_event_files(self, tmp_path):
        """bobi events reads events-*.jsonl files and merges them."""
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        ev1 = {"timestamp": "2026-01-01T00:00:01", "source": "github", "type": "push", "seq": 1, "deployment_id": "d1"}
        ev2 = {"timestamp": "2026-01-01T00:00:02", "source": "github", "type": "pr", "seq": 2, "deployment_id": "d1"}
        (state / "events-sess-a.jsonl").write_text(json.dumps(ev1) + "\n")
        (state / "events-sess-b.jsonl").write_text(json.dumps(ev2) + "\n")
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "push" in result.output
        assert "pr" in result.output

    def test_deduplicates_events_by_seq_deployment(self, tmp_path):
        """Same (seq, deployment_id) from different session files is shown once."""
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        ev = {"timestamp": "2026-01-01T00:00:01", "source": "github", "type": "push", "seq": 5, "deployment_id": "d1"}
        (state / "events-sess-a.jsonl").write_text(json.dumps(ev) + "\n")
        (state / "events-sess-b.jsonl").write_text(json.dumps(ev) + "\n")
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        # "push" should appear exactly once in the output
        assert result.output.count("push") == 1

    def test_inbox_event_renders_payload_text(self, tmp_path):
        """inbox/* events stored with 'payload' key surface their text."""
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        ev = {
            "timestamp": "2026-01-01T00:00:01",
            "source": "inbox",
            "type": "message",
            "payload": {"sender": "alice", "text": "hello world"},
        }
        (state / "events-sess-a.jsonl").write_text(json.dumps(ev) + "\n")
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "inbox" in result.output
        assert "alice" in result.output
        assert "hello world" in result.output

    def test_payload_event_renders_text_without_sender(self, tmp_path):
        """Non-inbox events stored with 'payload' key render text detail."""
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        ev = {
            "timestamp": "2026-01-01T00:00:01",
            "source": "github",
            "type": "push",
            "payload": {"text": "pushed to main"},
        }
        (state / "events-sess-a.jsonl").write_text(json.dumps(ev) + "\n")
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "pushed to main" in result.output

    def test_ignores_legacy_events_jsonl(self, tmp_path):
        """Legacy events.jsonl (without session prefix) is not read."""
        state = tmp_path / ".bobi" / "state"
        state.mkdir(parents=True)
        legacy = {"timestamp": "2026-01-01T00:00:01", "source": "github", "type": "legacy_push"}
        session = {"timestamp": "2026-01-01T00:00:02", "source": "github", "type": "new_pr", "seq": 1, "deployment_id": "d1"}
        (state / "events.jsonl").write_text(json.dumps(legacy) + "\n")
        (state / "events-sess-a.jsonl").write_text(json.dumps(session) + "\n")
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            result = runner.invoke(main, ["events"])
        assert result.exit_code == 0, result.output
        assert "legacy_push" not in result.output
        assert "new_pr" in result.output


class TestSetupCommand:
    """The setup command launches the local web UI."""

    @pytest.fixture(autouse=True)
    def _claude_present(self, monkeypatch):
        # The command preflights the Claude CLI; unit tests must pass on
        # machines without it.
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def test_missing_claude_cli_fails_with_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("bobi.sdk.get_cli_path",
                            lambda: "/nonexistent/claude")
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["setup"])
        assert result.exit_code != 0
        assert "Claude Code CLI" in result.output

    def test_interrupted_setup_requires_confirmation(self, tmp_path, monkeypatch):
        from bobi.setup.state import SetupState, Stage

        called = {}
        monkeypatch.setattr("bobi.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            SetupState(stage=Stage.DESIGN, team_name="t").save(Path(fs))
            declined = runner.invoke(main, ["setup"], input="n\n")
            assert declined.exit_code != 0
            assert "--resume" in declined.output
            assert "ran" not in called

    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "--resume" in result.output

    def test_runs_setup_against_literal_cwd(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run_setup(project_path, model=None, resume=False):
            seen.update(project=project_path, model=model, resume=resume)
            return 0

        monkeypatch.setattr("bobi.setup.run_setup", fake_run_setup)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            result = runner.invoke(main, ["setup", "--model", "sonnet"])
        assert result.exit_code == 0, result.output
        assert seen["project"] == Path(fs).resolve()
        assert seen["model"] == "sonnet"
        assert seen["resume"] is False

    def test_existing_install_requires_confirmation(self, tmp_path, monkeypatch):
        called = {}
        monkeypatch.setattr("bobi.setup.run_setup",
                            lambda *a, **k: called.setdefault("ran", True) and 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            dot = Path(".bobi")
            dot.mkdir()
            (dot / "agent.yaml").write_text("agent: eng-team\n")
            declined = runner.invoke(main, ["setup"], input="n\n")
            assert declined.exit_code != 0
            assert "ran" not in called
            accepted = runner.invoke(main, ["setup"], input="y\n")
            assert accepted.exit_code == 0, accepted.output
            assert called.get("ran") is True

    def test_resume_skips_confirmation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bobi.setup.run_setup", lambda *a, **k: 0)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            dot = Path(".bobi")
            dot.mkdir()
            (dot / "agent.yaml").write_text("agent: eng-team\n")
            result = runner.invoke(main, ["setup", "--resume"])
        assert result.exit_code == 0, result.output


# --- Discovery commands work without installation root ---------------------


class TestDiscoveryCommandsNoRoot:
    """Commands that should work on a fresh install (no .bobi/agent.yaml)."""

    def _no_root(self):
        """Patch that makes root detection fail, simulating a fresh install."""
        from click import UsageError
        return patch(
            "bobi.cli._detect_project_root",
            side_effect=UsageError("No bobi installation found"),
        )

    def test_agents_browse_without_root(self):
        runner = CliRunner()
        fake_remote = [{"name": "eng-team", "version": "1.0", "description": "test"}]
        with self._no_root(), \
             patch("bobi.registry.list_remote", return_value=fake_remote):
            result = runner.invoke(main, ["agents", "browse"])
        assert result.exit_code == 0, result.output
        assert "eng-team" in result.output

    def test_workflows_list_without_root(self):
        runner = CliRunner()
        with self._no_root():
            result = runner.invoke(main, ["workflows", "list"])
        assert result.exit_code == 0, result.output

    def test_workflows_validate_without_root(self, tmp_path):
        wf_file = tmp_path / "test.yaml"
        wf_file.write_text(
            "name: test-wf\ntrigger: manual\nsteps:\n"
            "  - name: s1\n    type: prompt\n    prompt: hello\n"
        )
        runner = CliRunner()
        with self._no_root():
            result = runner.invoke(main, ["workflows", "validate", str(wf_file)])
        assert result.exit_code == 0, result.output
        assert "Valid" in result.output

    def test_agents_list_requires_root(self):
        """Non-discovery commands should still fail without a root."""
        runner = CliRunner()
        with self._no_root():
            result = runner.invoke(main, ["agents", "list"])
        assert result.exit_code != 0


# --- bobi monitors add (weekly scheduling, #216 / MOD-216) ----------------


class TestMonitorAdd:
    """The `monitors add` CLI grows --at/--tz/--days/--notify so a weekly job
    is creatable without hand-editing YAML."""

    def _add(self, tmp_path, args):
        runner = CliRunner()
        with patch("bobi.cli._detect_project_root", return_value=tmp_path):
            return runner.invoke(main, ["monitors", "add", *args])

    def _written(self, tmp_path):
        import yaml
        path = tmp_path / ".bobi" / "monitors.yaml"
        return yaml.safe_load(path.read_text())["monitors"]

    def test_interval_monitor_still_works(self, tmp_path):
        result = self._add(tmp_path, ["pr check", "--interval", "15m",
                                      "--description", "check PRs"])
        assert result.exit_code == 0, result.output
        rec = self._written(tmp_path)[0]
        assert rec["name"] == "pr-check"
        assert rec["interval"] == "15m"
        assert "at" not in rec

    def test_weekly_notify_monitor_writes_at_days_tz(self, tmp_path):
        result = self._add(tmp_path, [
            "weekly-prep-doc", "--at", "21:00", "--days", "sun",
            "--tz", "America/Los_Angeles", "--notify",
            "--event", "monitor/prep.weekly_due",
            "--description", "Generate my prep doc for the upcoming week",
        ])
        assert result.exit_code == 0, result.output
        rec = self._written(tmp_path)[0]
        assert rec["name"] == "weekly-prep-doc"
        assert rec["at"] == ["21:00"]
        assert rec["days"] == ["sun"]
        assert rec["tz"] == "America/Los_Angeles"
        assert rec["notify"] is True
        assert rec["event"] == "monitor/prep.weekly_due"
        assert "interval" not in rec  # at-monitors don't serialize an interval

    def test_multiple_at_and_days(self, tmp_path):
        result = self._add(tmp_path, [
            "biweekly", "--at", "09:00", "--at", "17:00",
            "--days", "mon,wed,fri",
        ])
        assert result.exit_code == 0, result.output
        rec = self._written(tmp_path)[0]
        assert rec["at"] == ["09:00", "17:00"]
        assert rec["days"] == ["mon", "wed", "fri"]

    def test_interval_and_at_are_mutually_exclusive(self, tmp_path):
        result = self._add(tmp_path, ["x", "--interval", "5m", "--at", "21:00"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_days_without_at_is_rejected(self, tmp_path):
        result = self._add(tmp_path, ["x", "--days", "sun"])
        assert result.exit_code != 0
        assert "--days only applies to --at" in result.output

    def test_invalid_at_time_is_rejected(self, tmp_path):
        result = self._add(tmp_path, ["x", "--at", "25:00"])
        assert result.exit_code != 0
        assert "at-time" in result.output

    def test_invalid_weekday_is_rejected(self, tmp_path):
        result = self._add(tmp_path, ["x", "--at", "21:00", "--days", "funday"])
        assert result.exit_code != 0
        assert "weekday" in result.output.lower()
