"""CLI contract tests for the Bobi Agent command tree."""

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bobi.__version__ import __version__
from bobi import paths
from bobi.cli import main
from bobi.subagent import CheckResult, GateResult
from tests.conftest import TEST_AGENT_NAME


def test_version_flag():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "bobi" in result.output
    assert __version__ in result.output


def test_bare_bobi_starts_app(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "bobi.webapp.daemon.start",
        lambda open_browser=True: calls.append(open_browser)
        or type("Status", (), {"url": "http://127.0.0.1:8642/?n=tok",
                               "pid": 1234})(),
    )

    result = CliRunner().invoke(main, [])

    assert result.exit_code == 0, result.output
    assert calls == [True]
    assert "bobi app is running at http://127.0.0.1:8642/?n=tok" in result.output


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


def test_agent_group_pins_team_brain_for_cli_process(bobi_install, monkeypatch):
    """`bobi agent <name> ...` must select the team's brain for sessions the
    CLI process itself runs - a gateway team's `--wait` completion check
    otherwise hits real Anthropic with the gateway's token (#655)."""
    import os
    import yaml

    for var in ("BOBI_BRAIN", "BOBI_BRAIN_MODEL",
                "BOBI_GATEWAY_BASE_URL", "BOBI_GATEWAY_SMALL_MODEL",
                "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    agent_yaml = bobi_install.repo_path / "package" / "agent.yaml"
    cfg = yaml.safe_load(agent_yaml.read_text())
    cfg["brain"] = {"kind": "gateway", "base_url": "http://localhost:4000",
                    "model": "qwen3:14b"}
    agent_yaml.write_text(yaml.dump(cfg))
    (bobi_install.repo_path / ".env").write_text(
        "ANTHROPIC_AUTH_TOKEN=from-runtime-dotenv\n")

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "status"])

    assert result.exit_code == 0, result.output
    assert os.environ.get("BOBI_BRAIN") == "gateway"
    assert os.environ.get("BOBI_BRAIN_MODEL") == "qwen3:14b"
    assert os.environ.get("BOBI_GATEWAY_BASE_URL") == "http://localhost:4000"
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "from-runtime-dotenv"


def test_missing_agent_errors_without_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(main, ["agent", "missing", "status"])
    assert result.exit_code != 0
    assert "Bobi Agent 'missing' is not installed" in result.output
    assert "package/agent.yaml" in result.output


def test_agent_ui_deployment_mode_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        main, ["agent", "canary", "ui", "ci-canary"])

    assert result.exit_code != 0
    assert "`bobi agent <name> ui <deployment>` was removed" in result.output
    assert "control plane" in result.output


def test_agent_ui_removed_app_flag_reports_control_plane(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        main, ["agent", "canary", "ui", "--app", "ci-canary", "--check"])

    assert result.exit_code != 0
    assert "`bobi agent <name> ui <deployment>` was removed" in result.output
    assert "control plane" in result.output


def test_agent_ui_local_deep_links_unified_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    opened = {}

    monkeypatch.setattr(
        "bobi.webapp.daemon.start",
        lambda open_browser=True: type(
            "Status", (), {"url": "http://127.0.0.1:8642/?n=tok",
                           "pid": 1234})(),
    )
    monkeypatch.setattr("webbrowser.open",
                        lambda url: opened.setdefault("url", url))

    result = CliRunner().invoke(main, ["agent", "canary", "ui"])

    assert result.exit_code == 0, result.output
    assert opened["url"] == "http://127.0.0.1:8642/?n=tok#/agents/canary"


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


class TestMonitorGate:
    """`monitors gate` is the scheduler's relevance-gate plumbing (#630):
    read the request file, run the gate agent, print the verdict line."""

    def _request(self, tmp_path, **overrides):
        payload = {"criterion": "about billing", "name": "gate-billing",
                   "items": [{"key": "m1", "data": {"subject": "refund"}}]}
        payload.update(overrides)
        req = tmp_path / "req.json"
        req.write_text(json.dumps(payload))
        return req

    def test_prints_verdict_line(self, bobi_install, tmp_path):
        gate = GateResult(success=True, relevant=["m1"])
        with patch("bobi.subagent.run_gate_blocking", return_value=gate) as mock:
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "monitors", "gate",
                "--request", str(self._request(tmp_path)),
            ])
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output.strip().splitlines()[-1])
        assert verdict == {"success": True, "relevant": ["m1"]}
        assert mock.call_args[0][0] == "about billing"
        assert mock.call_args[0][1] == [{"key": "m1",
                                         "data": {"subject": "refund"}}]
        assert mock.call_args[1]["name"] == "gate-billing"

    def test_gate_failure_exits_nonzero_with_verdict(self, bobi_install, tmp_path):
        gate = GateResult(success=False, error="no verdict")
        with patch("bobi.subagent.run_gate_blocking", return_value=gate):
            result = CliRunner().invoke(main, [
                "agent", TEST_AGENT_NAME, "monitors", "gate",
                "--request", str(self._request(tmp_path)),
            ])
        assert result.exit_code == 1
        # The verdict line still prints so the scheduler parses "success": false.
        assert '"success": false' in result.output

    def test_missing_request_file_fails(self, bobi_install, tmp_path):
        result = CliRunner().invoke(main, [
            "agent", TEST_AGENT_NAME, "monitors", "gate",
            "--request", str(tmp_path / "nope.json"),
        ])
        assert result.exit_code == 1

    def test_empty_items_rejected(self, bobi_install, tmp_path):
        result = CliRunner().invoke(main, [
            "agent", TEST_AGENT_NAME, "monitors", "gate",
            "--request", str(self._request(tmp_path, items=[])),
        ])
        assert result.exit_code == 1


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

    def test_publish_reads_payload_from_stdin(self, bobi_install):
        with patch("bobi.events.publish.post_event", return_value=True) as post:
            result = CliRunner().invoke(
                main,
                ["agent", TEST_AGENT_NAME, "events", "publish", "alert/firing"],
                input='{"title":"x"}',
            )

        assert result.exit_code == 0, result.output
        assert "Published alert/firing" in result.output
        post.assert_called_once_with(
            "alert/firing",
            {"title": "x"},
            project_path=bobi_install.repo_path,
        )

    def test_publish_reads_payload_from_json_option(self, bobi_install):
        with patch("bobi.events.publish.post_event", return_value=True) as post:
            result = CliRunner().invoke(
                main,
                [
                    "agent", TEST_AGENT_NAME, "events", "publish",
                    "alert/firing", "--json", '{"title":"x"}',
                ],
            )

        assert result.exit_code == 0, result.output
        post.assert_called_once_with(
            "alert/firing",
            {"title": "x"},
            project_path=bobi_install.repo_path,
        )

    def test_publish_rejects_non_object_payload(self, bobi_install):
        result = CliRunner().invoke(
            main,
            ["agent", TEST_AGENT_NAME, "events", "publish", "alert/firing"],
            input='["x"]',
        )

        assert result.exit_code != 0
        assert "Payload must be a JSON object" in result.output

    def test_publish_rejects_bare_topic(self, bobi_install):
        result = CliRunner().invoke(
            main,
            [
                "agent", TEST_AGENT_NAME, "events", "publish",
                "firing", "--json", '{"title":"x"}',
            ],
        )

        assert result.exit_code != 0
        assert "source/type" in result.output

    def test_publish_rejects_global_topic_prefixes(self, bobi_install):
        for topic in [
            "github:org/repo",
            "linear:TEAM/firing",
            "slack:T123/firing",
            "alert/github:org",
        ]:
            result = CliRunner().invoke(
                main,
                [
                    "agent", TEST_AGENT_NAME, "events", "publish",
                    topic, "--json", '{"title":"x"}',
                ],
            )

            assert result.exit_code != 0
            assert "reserved for webhooks" in result.output

    def test_publish_rejects_webhook_source_labels(self, bobi_install):
        for topic in [
            "github/firing",
            "linear/firing",
            "slack/firing",
        ]:
            result = CliRunner().invoke(
                main,
                [
                    "agent", TEST_AGENT_NAME, "events", "publish",
                    topic, "--json", '{"title":"x"}',
                ],
            )

            assert result.exit_code != 0
            assert "sources are reserved for webhooks" in result.output

    def test_publish_without_payload_does_not_read_interactive_stdin(
        self,
        bobi_install,
        monkeypatch,
    ):
        class TtyStdin:
            @staticmethod
            def isatty():
                return True

            @staticmethod
            def read():
                raise AssertionError("interactive stdin should not be read")

        monkeypatch.setattr("click.get_text_stream", lambda name: TtyStdin())
        result = CliRunner().invoke(
            main,
            ["agent", TEST_AGENT_NAME, "events", "publish", "alert/firing"],
        )

        assert result.exit_code != 0
        assert "Provide payload with --json or stdin" in result.output

    def test_publish_reports_rejected_publish(self, bobi_install):
        with patch("bobi.events.publish.post_event", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "agent", TEST_AGENT_NAME, "events", "publish",
                    "alert/firing", "--json", '{"title":"x"}',
                ],
            )

        assert result.exit_code != 0
        assert "Publish failed" in result.output
        assert "bubble credentials" in result.output


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

    def _patch_app(self, monkeypatch):
        seen = {}

        monkeypatch.setattr(
            "bobi.webapp.daemon.start",
            lambda open_browser=True: seen.setdefault("open_browser", open_browser)
            or type("Status", (), {"url": "http://127.0.0.1:8642/?n=tok",
                                   "pid": 1234})(),
        )
        monkeypatch.setattr("webbrowser.open",
                            lambda url: seen.setdefault("url", url))
        return seen

    def test_setup_opens_named_unified_app_route(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        seen = self._patch_app(monkeypatch)

        result = CliRunner().invoke(main, ["setup", "alpha"])

        assert result.exit_code == 0, result.output
        assert seen["open_browser"] is False
        assert seen["url"] == "http://127.0.0.1:8642/?n=tok#/setup/alpha"
        assert "bobi setup is open at" in result.output

    def test_help(self):
        result = CliRunner().invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "--resume" in result.output

    def test_setup_without_name_opens_create_route(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        seen = self._patch_app(monkeypatch)

        result = CliRunner().invoke(main, ["setup"])

        assert result.exit_code == 0, result.output
        assert seen["url"] == "http://127.0.0.1:8642/?n=tok#/setup"

    def test_setup_options_are_accepted_for_compatibility(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        seen = self._patch_app(monkeypatch)

        result = CliRunner().invoke(
            main, ["setup", "alpha", "--resume", "--model", "sonnet"])

        assert result.exit_code == 0, result.output
        assert seen["url"] == (
            "http://127.0.0.1:8642/?n=tok#/setup/alpha?model=sonnet")


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
