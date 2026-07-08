"""Tests for the notify workflow step type — schema, orchestrator, and Slack posting."""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import httpx
import pytest

from bobi import paths
from bobi import http as pooled

from bobi.workflow.schema import (
    Workflow, StepDef, HandoffContract, load_workflow,
)
from bobi.workflow.orchestrator import (
    _execute_notify_step,
    run_workflow,
)
from bobi.workflow.variables import VariableContext
from bobi.slack import format_slack_message, post_slack_message


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

class TestNotifyStepSchema:
    def test_load_notify_step(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: test-wf
            steps:
              - name: notify_start
                notify: slack
                message: "Working on #42: fix the bug"
        """))
        wf = load_workflow(f)
        assert len(wf.steps) == 1
        step = wf.steps[0]
        assert step.name == "notify_start"
        assert step.notify == "slack"
        assert step.message == "Working on #42: fix the bug"
        # Not a prompt step
        assert step.prompt == ""
        assert step.agent == ""

    def test_notify_step_with_variables(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: test-wf
            steps:
              - name: greet
                notify: slack
                message: "Working on #${{input.run_key}}: ${{input.task}}"
        """))
        wf = load_workflow(f)
        step = wf.steps[0]
        assert "${{input.run_key}}" in step.message
        assert "${{input.task}}" in step.message

    def test_notify_step_coexists_with_other_types(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(textwrap.dedent("""\
            name: mixed-wf
            steps:
              - name: work
                agent: engineer
                prompt: "Do the thing"
                handoff:
                  required: [status]
              - name: notify_done
                notify: slack
                message: "Done!"
              - name: route
                if: "status == done"
                goto: work
                else: work
                max_iterations: 1
        """))
        wf = load_workflow(f)
        assert len(wf.steps) == 3
        assert wf.steps[0].prompt == "Do the thing"
        assert wf.steps[1].notify == "slack"
        assert wf.steps[2].condition == "status == done"


# ---------------------------------------------------------------------------
# format_slack_message (shared module)
# ---------------------------------------------------------------------------

class TestFormatSlackMessage:
    def test_escaped_newlines(self):
        assert format_slack_message("a\\nb") == "a\nb"

    def test_heading_to_bold(self):
        assert format_slack_message("# Hello") == "*Hello*"

    def test_bold_markdown(self):
        assert format_slack_message("**bold**") == "*bold*"

    def test_link_conversion(self):
        result = format_slack_message("[click](https://example.com)")
        assert result == "<https://example.com|click>"

    def test_unordered_list_dash(self):
        assert format_slack_message("- item one\n- item two") == "• item one\n• item two"

    def test_unordered_list_asterisk(self):
        assert format_slack_message("* item one\n* item two") == "• item one\n• item two"

    def test_nested_list(self):
        result = format_slack_message("- top\n  - nested")
        assert result == "• top\n  • nested"

    def test_truncation(self):
        long_text = "x" * 4000
        result = format_slack_message(long_text)
        assert len(result) <= 3020  # 3000 + truncation suffix
        assert "_(truncated)_" in result


# ---------------------------------------------------------------------------
# post_slack_message
# ---------------------------------------------------------------------------

class TestPostSlackMessage:
    def test_basic_post(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            result = post_slack_message("xoxb-test", "C123", "Hello")

        assert result["ok"] is True
        assert captured["body"]["channel"] == "C123"
        assert captured["body"]["text"] == "Hello"
        assert "thread_ts" not in captured["body"]

    def test_thread_reply(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            post_slack_message("xoxb-test", "C123", "Reply", thread_ts="171.42")

        assert captured["body"]["thread_ts"] == "171.42"

    def test_api_error_raises(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            with pytest.raises(RuntimeError, match="channel_not_found"):
                post_slack_message("xoxb-test", "C123", "Hello")


# ---------------------------------------------------------------------------
# _execute_notify_step
# ---------------------------------------------------------------------------

class TestExecuteNotifyStep:
    def _make_ctx(self, run_key="42", task="fix the bug",
                  channel="C123", thread_ts="171.42", workspace="T999"):
        ctx = VariableContext()
        ctx.set_scope("input", {"task": task, "repo": "test/repo", "run_key": run_key})
        if channel:
            ctx.set_scope("requested_by", {
                "channel": channel,
                "thread_ts": thread_ts,
                "workspace": workspace,
            })
        return ctx

    @patch("bobi.slack.post_slack_message")
    @patch("bobi.workflow.orchestrator._emit_lifecycle_event")
    def test_posts_to_slack(self, mock_emit, mock_post, tmp_path, monkeypatch):
        self._setup_config(tmp_path, monkeypatch)

        step = StepDef(name="notify_start", notify="slack",
                       message="Working on #${{input.run_key}}: ${{input.task}}")
        ctx = self._make_ctx()

        _execute_notify_step(step, ctx, str(tmp_path), "42", "issue-lifecycle")

        mock_post.assert_called_once_with(
            "xoxb-test", "C123", "Working on #42: fix the bug",
            thread_ts="171.42",
        )
        # Lifecycle event emitted
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "engineer/notify.sent"

    @patch("bobi.slack.post_slack_message")
    @patch("bobi.workflow.orchestrator._emit_lifecycle_event")
    def test_skips_when_no_channel(self, mock_emit, mock_post, tmp_path, monkeypatch):
        """No channel in requested_by → skip without calling Slack."""
        self._setup_config(tmp_path, monkeypatch)

        step = StepDef(name="notify_start", notify="slack", message="Hello")
        ctx = VariableContext()
        ctx.set_scope("input", {"task": "t", "repo": "r", "run_key": "1"})
        # No requested_by scope → no channel

        _execute_notify_step(step, ctx, str(tmp_path), "1", "test-wf")

        mock_post.assert_not_called()
        mock_emit.assert_not_called()

    @patch("bobi.slack.post_slack_message")
    @patch("bobi.workflow.orchestrator._emit_lifecycle_event")
    def test_skips_when_no_token(self, mock_emit, mock_post, tmp_path, monkeypatch):
        # Config without slack token
        paths.package_dir(tmp_path).mkdir(parents=True)
        paths.agent_yaml_path(tmp_path).write_text("entry_point: manager\n")
        paths.bind_root(tmp_path)

        step = StepDef(name="notify_start", notify="slack", message="Hello")
        ctx = self._make_ctx()

        _execute_notify_step(step, ctx, str(tmp_path), "1", "test-wf")

        mock_post.assert_not_called()

    @patch("bobi.slack.post_slack_message")
    @patch("bobi.workflow.orchestrator._emit_lifecycle_event")
    def test_unknown_notify_target_skips(self, mock_emit, mock_post, tmp_path, monkeypatch):
        self._setup_config(tmp_path, monkeypatch)

        step = StepDef(name="notify_start", notify="email", message="Hello")
        ctx = self._make_ctx()

        _execute_notify_step(step, ctx, str(tmp_path), "1", "test-wf")

        mock_post.assert_not_called()

    @patch("bobi.slack.post_slack_message",
           side_effect=RuntimeError("network error"))
    @patch("bobi.workflow.orchestrator._emit_lifecycle_event")
    def test_slack_failure_is_non_fatal(self, mock_emit, mock_post, tmp_path, monkeypatch):
        self._setup_config(tmp_path, monkeypatch)

        step = StepDef(name="notify_start", notify="slack", message="Hello")
        ctx = self._make_ctx()

        # Should not raise
        _execute_notify_step(step, ctx, str(tmp_path), "42", "test-wf")

        # Failure event emitted
        assert any(
            call[0][0] == "engineer/notify.failed"
            for call in mock_emit.call_args_list
        )

    def _setup_config(self, tmp_path, monkeypatch):
        config_dir = paths.package_dir(tmp_path)
        config_dir.mkdir(parents=True, exist_ok=True)
        paths.agent_yaml_path(tmp_path).write_text(
            "entry_point: manager\n"
            "services:\n"
            "  - name: slack\n"
            "    credentials:\n"
            "      bot_token: 'xoxb-test'\n"
        )
        paths.bind_root(tmp_path)


# ---------------------------------------------------------------------------
# Orchestrator integration — notify steps are skipped by the LLM loop
# ---------------------------------------------------------------------------

@dataclass
class FakeResultMessage:
    session_id: str = "test-session-id"
    duration_ms: int = 1000
    total_cost_usd: float = 0.01
    num_turns: int = 1
    is_error: bool = False
    result: str = ""
    deferred_tool_use: object = None


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeAssistantMessage:
    content: list


class FakeClient:
    def __init__(self):
        self.queries = []
        self.connected = False

    async def connect(self, prompt=None):
        self.connected = True
        if prompt:
            self.queries.append(prompt)

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        yield FakeAssistantMessage(content=[FakeTextBlock(text="Done.")])
        yield FakeResultMessage()

    async def disconnect(self):
        self.connected = False


class TestNotifyStepInWorkflow:
    def _mock_asyncio_run(self, workflow, tmp_path=None, **kwargs):
        cwd = kwargs.get("cwd", "/tmp")
        with patch("bobi.workflow.orchestrator.get_registry") as mock_reg, \
             patch("bobi.workflow.orchestrator._emit_lifecycle_event"), \
             patch("bobi.workflow.orchestrator._setup_worktree", return_value=cwd), \
             patch("bobi.workflow.orchestrator.load_session_id", return_value=""), \
             patch("bobi.workflow.orchestrator.save_session_id"), \
             patch("bobi.workflow.orchestrator.log_activity"), \
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch("bobi.workflow.orchestrator._execute_notify_step") as mock_notify, \
             patch("bobi.workflow.orchestrator._find_project_root", return_value=Path(tmp_path or "/tmp")), \
             patch.dict("sys.modules", {"claude_agent_sdk": MagicMock(
                 ClaudeSDKClient=lambda opts: FakeClient(),
                 ClaudeAgentOptions=MagicMock,
                 AssistantMessage=FakeAssistantMessage,
                 ResultMessage=FakeResultMessage,
                 TextBlock=FakeTextBlock,
             )}):
            mock_reg.return_value = MagicMock()
            result = run_workflow(workflow, **kwargs)
            return result, mock_notify

    def test_notify_step_executed_not_sent_to_llm(self, tmp_path, monkeypatch):
        """Notify steps call _execute_notify_step and do not query the LLM."""
        paths.bind_root(tmp_path)
        paths.sessions_dir(tmp_path)

        wf = Workflow(name="t", steps=[
            StepDef(name="notify_start", notify="slack",
                    message="Working on it"),
            StepDef(name="work", prompt="Do the thing"),
        ])
        result, mock_notify = self._mock_asyncio_run(
            wf, tmp_path=tmp_path, task="t", repo="r", cwd="/tmp", run_key="1",
        )
        assert result is True
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[0][0].name == "notify_start"

    def test_workflow_with_notify_at_both_ends(self, tmp_path, monkeypatch):
        """Workflow with notify at start and end completes successfully."""
        root = tmp_path / "_repo"
        paths.bind_root(root)
        sessions = paths.sessions_dir(root)

        original_init = FakeClient.__init__

        def _patched_init(self_client):
            original_init(self_client)
            d = sessions / "wf-t-r-1"
            d.mkdir(parents=True, exist_ok=True)
            (d / "handoff-work.yaml").write_text("status: done\n")

        monkeypatch.setattr(FakeClient, "__init__", _patched_init)

        wf = Workflow(name="t", steps=[
            StepDef(name="notify_start", notify="slack",
                    message="Starting"),
            StepDef(name="work", prompt="Do the thing",
                    handoff=HandoffContract(required=["status"])),
            StepDef(name="notify_end", notify="slack",
                    message="Done"),
        ])
        result, mock_notify = self._mock_asyncio_run(
            wf, tmp_path=tmp_path, task="t", repo="r", cwd="/tmp", run_key="1",
        )
        assert result is True
        assert mock_notify.call_count == 2
        step_names = [c[0][0].name for c in mock_notify.call_args_list]
        assert step_names == ["notify_start", "notify_end"]


# ---------------------------------------------------------------------------
# Issue-lifecycle workflow structure
# ---------------------------------------------------------------------------

class TestIssueLifecycleNotifySteps:
    def test_has_notify_start_step(self):
        wf_path = Path(__file__).parent.parent / "agents" / "eng-team" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not found")
        wf = load_workflow(wf_path)

        step = wf.step_by_name("notify_start")
        assert step is not None, "notify_start step must exist"
        assert step.notify == "slack"
        assert "input.run_key" in step.message
        assert "input.task" in step.message

    def test_has_notify_complete_step(self):
        wf_path = Path(__file__).parent.parent / "agents" / "eng-team" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not found")
        wf = load_workflow(wf_path)

        step = wf.step_by_name("notify_complete")
        assert step is not None, "notify_complete step must exist"
        assert step.notify == "slack"
        assert "input.run_key" in step.message

    def test_notify_start_after_pickup_before_route(self):
        wf_path = Path(__file__).parent.parent / "agents" / "eng-team" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not found")
        wf = load_workflow(wf_path)

        pickup_idx = wf.step_index("pickup")
        notify_idx = wf.step_index("notify_start")
        route_idx = wf.step_index("route")
        assert pickup_idx < notify_idx < route_idx, \
            "notify_start must be between pickup and route"

    def test_notify_complete_is_last_step(self):
        wf_path = Path(__file__).parent.parent / "agents" / "eng-team" / "workflows" / "issue-lifecycle.yaml"
        if not wf_path.exists():
            pytest.skip("issue-lifecycle.yaml not found")
        wf = load_workflow(wf_path)

        notify_idx = wf.step_index("notify_complete")
        assert notify_idx == len(wf.steps) - 1, \
            "notify_complete must be the last step"
