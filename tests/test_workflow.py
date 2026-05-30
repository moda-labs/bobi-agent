"""Tests for the workflow engine — schema, variables, engine execution."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from modastack.workflow.schema import (
    NodeDef, NodeType, WorkflowDef, TriggerDef, WaitForDef,
    BranchDef, ListenForDef, load_workflow,
)
from modastack.workflow.variables import VariableContext
from modastack.workflow.state import WorkflowRun, NodeState
from modastack.workflow.actions import ActionRegistry, build_registry
from modastack.workflow.engine import WorkflowEngine
from modastack.workflow.triggers import WorkflowDispatcher


# === Schema Tests ===

class TestTriggerDef:
    def test_matches_event_type(self):
        trigger = TriggerDef(event="task.assigned")
        assert trigger.matches({"type": "task.assigned", "data": {}})
        assert not trigger.matches({"type": "task.created", "data": {}})

    def test_matches_with_filter(self):
        trigger = TriggerDef(event="task.assigned", filter={"repo": "modastack"})
        assert trigger.matches({"type": "task.assigned", "data": {"repo": "modastack"}})
        assert not trigger.matches({"type": "task.assigned", "data": {"repo": "other"}})

    def test_matches_list_filter(self):
        trigger = TriggerDef(event="task.assigned", filter={"labels": ["agent"]})
        assert trigger.matches({"type": "task.assigned", "data": {"labels": ["agent", "bug"]}})
        assert not trigger.matches({"type": "task.assigned", "data": {"labels": ["bug"]}})


class TestTopologicalSort:
    def test_simple_chain(self):
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo a"),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo b", depends_on=["a"]),
            "c": NodeDef(id="c", type=NodeType.BASH, command="echo c", depends_on=["b"]),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        order = wf.topological_order()
        assert order == ["a", "b", "c"]

    def test_parallel_nodes(self):
        nodes = {
            "root": NodeDef(id="root", type=NodeType.BASH, command="echo root"),
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo a", depends_on=["root"]),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo b", depends_on=["root"]),
            "join": NodeDef(id="join", type=NodeType.BASH, command="echo join", depends_on=["a", "b"]),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        order = wf.topological_order()
        assert order[0] == "root"
        assert order[-1] == "join"
        assert set(order[1:3]) == {"a", "b"}

    def test_cycle_detection(self):
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo a", depends_on=["b"]),
            "b": NodeDef(id="b", type=NodeType.BASH, command="echo b", depends_on=["a"]),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        with pytest.raises(ValueError, match="Cycle"):
            wf.topological_order()


class TestValidation:
    def test_unknown_dependency(self):
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo a", depends_on=["nonexistent"]),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        errors = wf.validate()
        assert any("nonexistent" in e for e in errors)

    def test_gate_without_branches(self):
        nodes = {
            "g": NodeDef(id="g", type=NodeType.GATE),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        errors = wf.validate()
        assert any("no branches" in e for e in errors)

    def test_valid_workflow_passes(self):
        nodes = {
            "a": NodeDef(id="a", type=NodeType.BASH, command="echo hi"),
        }
        wf = WorkflowDef(name="test", version=1, trigger=TriggerDef(event="test"), nodes=nodes)
        assert wf.validate() == []


class TestLoadWorkflow:
    def test_load_issue_lifecycle(self):
        path = Path(__file__).parent.parent / "workflows" / "issue-lifecycle.yaml"
        wf = load_workflow(path)
        assert wf.name == "issue-lifecycle"
        assert wf.trigger.event == "task.assigned"
        assert len(wf.nodes) == 23
        assert "spawn_engineer" in wf.nodes
        assert wf.nodes["spawn_engineer"].type == NodeType.ACTION
        assert wf.nodes["craft_pickup_message"].type == NodeType.MANAGER
        assert wf.nodes["post_slack_pickup"].type == NodeType.ACTION
        assert wf.nodes["triage"].type == NodeType.PROMPT
        assert wf.nodes["route"].type == NodeType.GATE
        assert wf.nodes["spec_approval"].type == NodeType.APPROVAL


# === Variable Tests ===

class TestVariableContext:
    def test_basic_resolution(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"issue_id": "BET-15", "title": "Add auth"})
        assert ctx.resolve("Issue ${{event.issue_id}}: ${{event.title}}") == "Issue BET-15: Add auth"

    def test_pipe_filter_lower(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"issue_id": "BET-15"})
        assert ctx.resolve("${{event.issue_id | lower}}") == "bet-15"

    def test_pipe_filter_upper(self):
        ctx = VariableContext()
        ctx.set_scope("event", {"issue_id": "bet-15"})
        assert ctx.resolve("${{event.issue_id | upper}}") == "BET-15"

    def test_missing_key_returns_empty(self):
        ctx = VariableContext()
        ctx.set_scope("event", {})
        assert ctx.resolve("${{event.missing}}") == ""

    def test_unresolvable_scope_preserved(self):
        ctx = VariableContext()
        assert ctx.resolve("${{unknown.key}}") == ""

    def test_chained_scope(self):
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "medium"})
        ctx.set_scope("event", {"issue_id": "42"})
        result = ctx.resolve("${{event.issue_id}} is ${{triage.complexity}}")
        assert result == "42 is medium"


class TestConditionEvaluator:
    def test_equality(self):
        ctx = VariableContext()
        ctx.set_scope("route", {"branch": "needs_spec"})
        assert ctx.evaluate_condition("${{route.branch}} == 'needs_spec'")
        assert not ctx.evaluate_condition("${{route.branch}} == 'skip_spec'")

    def test_inequality(self):
        ctx = VariableContext()
        ctx.set_scope("route", {"branch": "needs_spec"})
        assert ctx.evaluate_condition("${{route.branch}} != 'skip_spec'")

    def test_in_operator(self):
        ctx = VariableContext()
        ctx.set_scope("assess", {"output": "I recommend spec"})
        assert ctx.evaluate_condition("'spec' in ${{assess.output}}")
        assert not ctx.evaluate_condition("'implement' in ${{assess.output}}")

    def test_and_operator(self):
        ctx = VariableContext()
        ctx.set_scope("a", {"x": "true"})
        ctx.set_scope("b", {"y": "true"})
        assert ctx.evaluate_condition("${{a.x}} == 'true' and ${{b.y}} == 'true'")

    def test_or_operator(self):
        ctx = VariableContext()
        ctx.set_scope("a", {"approved": "true"})
        ctx.set_scope("route", {"branch": "skip_spec"})
        assert ctx.evaluate_condition(
            "${{a.approved}} == true or ${{route.branch}} == 'skip_spec'"
        )

    def test_in_list(self):
        ctx = VariableContext()
        ctx.set_scope("triage", {"complexity": "medium"})
        assert ctx.evaluate_condition(
            "${{triage.complexity}} in ['medium', 'large']"
        )


# === State Tests ===

class TestWorkflowState:
    def test_create_and_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        event = {"type": "task.assigned", "data": {"issue_id": "42"}}
        run = WorkflowRun.create("test-wf", event)
        run.save()

        loaded = WorkflowRun.load(run.run_id)
        assert loaded.workflow_name == "test-wf"
        assert loaded.trigger_event == event

    def test_find_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        event = {"type": "task.assigned", "data": {"issue_id": "42"}}
        run = WorkflowRun.create("test-wf", event)
        run.status = "running"
        run.save()

        found = WorkflowRun.find_active("test-wf", "42")
        assert found is not None
        assert found.run_id == run.run_id

    def test_find_active_ignores_completed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        event = {"type": "task.assigned", "data": {"issue_id": "42"}}
        run = WorkflowRun.create("test-wf", event)
        run.status = "completed"
        run.save()

        found = WorkflowRun.find_active("test-wf", "42")
        assert found is None

    def test_node_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        ns = run.node_state("step1")
        ns.status = "completed"
        ns.outputs = {"result": "ok"}
        run.save()

        loaded = WorkflowRun.load(run.run_id)
        assert loaded.nodes["step1"].status == "completed"
        assert loaded.nodes["step1"].outputs == {"result": "ok"}


# === Engine Tests ===

class TestEngineExecution:
    def _make_workflow(self, nodes: dict[str, NodeDef]) -> WorkflowDef:
        return WorkflowDef(
            name="test", version=1,
            trigger=TriggerDef(event="test"),
            nodes=nodes,
        )

    def test_bash_node_executes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "step": NodeDef(id="step", type=NodeType.BASH, command="echo hello"),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.status == "completed"
        assert run.nodes["step"].status == "completed"
        assert run.nodes["step"].outputs["stdout"] == "hello"

    def test_action_node_executes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        registry = ActionRegistry()
        registry.register("test.echo", lambda params: {"echoed": params["msg"]})

        nodes = {
            "step": NodeDef(id="step", type=NodeType.ACTION,
                          action="test.echo", params={"msg": "hi"}),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run, registry=registry)
        engine.execute()

        assert run.nodes["step"].outputs["echoed"] == "hi"

    def test_gate_selects_branch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo spec"),
            "gate": NodeDef(
                id="gate", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "spec": BranchDef(when="'spec' in ${{input.stdout}}"),
                    "impl": BranchDef(when="'implement' in ${{input.stdout}}"),
                },
                fallback="spec",
            ),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.nodes["gate"].outputs["branch"] == "spec"

    def test_conditional_skip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo skip"),
            "maybe": NodeDef(
                id="maybe", type=NodeType.BASH, command="echo ran",
                depends_on=["input"],
                when="${{input.stdout}} == 'run'",
            ),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.nodes["maybe"].status == "skipped"

    def test_dependency_ordering(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "first": NodeDef(id="first", type=NodeType.BASH, command="echo 1"),
            "second": NodeDef(id="second", type=NodeType.BASH, command="echo 2",
                            depends_on=["first"]),
            "third": NodeDef(id="third", type=NodeType.BASH, command="echo 3",
                           depends_on=["second"]),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.status == "completed"
        for nid in ("first", "second", "third"):
            assert run.nodes[nid].status == "completed"

    def test_failed_bash_marks_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "fail": NodeDef(id="fail", type=NodeType.BASH, command="exit 1"),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.nodes["fail"].status == "failed"
        assert run.status == "failed"

    def test_variable_substitution_in_bash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "step": NodeDef(id="step", type=NodeType.BASH,
                          command="echo ${{event.name}}"),
        }
        wf = self._make_workflow(nodes)
        event = {"type": "test", "data": {"name": "world"}}
        run = WorkflowRun.create("test", event)
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.nodes["step"].outputs["stdout"] == "world"

    def test_gate_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        nodes = {
            "input": NodeDef(id="input", type=NodeType.BASH, command="echo garbage"),
            "gate": NodeDef(
                id="gate", type=NodeType.GATE,
                depends_on=["input"],
                branches={
                    "a": BranchDef(when="'nope' in ${{input.stdout}}"),
                    "b": BranchDef(when="'nah' in ${{input.stdout}}"),
                },
                fallback="a",
            ),
        }
        wf = self._make_workflow(nodes)
        run = WorkflowRun.create("test", {"type": "test", "data": {}})
        engine = WorkflowEngine(wf, run)
        engine.execute()

        assert run.nodes["gate"].outputs["branch"] == "a"


class TestRepoScope:
    """Test that per-repo context from .modastack.yaml is injected as ${{repo.key}}."""

    def _make_workflow(self, nodes):
        return WorkflowDef(
            name="test", version=1,
            trigger=TriggerDef(event="test"),
            nodes=nodes,
        )

    def test_repo_context_injected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        # Create a repo with .modastack.yaml containing context
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / ".modastack.yaml").write_text(yaml.dump({
            "task_tracking": {"system": "github-issues", "project": "TEST"},
            "context": {
                "content_dir": "docs/blog",
                "publish_command": "npm run publish",
            },
        }))

        # Mock GlobalConfig to return our repo
        mock_config = MagicMock()
        mock_config.repos = [repo_dir]
        mock_config.slack_bot_token = ""
        mock_config.slack_dm_channel = ""
        with patch("modastack.config.GlobalConfig.load", return_value=mock_config):
            nodes = {
                "step": NodeDef(id="step", type=NodeType.BASH,
                               command="echo ${{repo.content_dir}}"),
            }
            wf = self._make_workflow(nodes)
            event = {"type": "test", "data": {"repo": str(repo_dir)}}
            run = WorkflowRun.create("test", event)
            engine = WorkflowEngine(wf, run)
            engine.execute()

        assert run.nodes["step"].outputs["stdout"] == "docs/blog"

    def test_repo_builtin_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / ".modastack.yaml").write_text(yaml.dump({
            "task_tracking": {"system": "github-issues", "project": "DOCS"},
            "verify": {"test_command": "pytest"},
            "context": {},
        }))

        mock_config = MagicMock()
        mock_config.repos = [repo_dir]
        mock_config.slack_bot_token = ""
        mock_config.slack_dm_channel = ""

        with patch("modastack.config.GlobalConfig.load", return_value=mock_config):
            nodes = {
                "step": NodeDef(id="step", type=NodeType.BASH,
                               command="echo ${{repo.project}}"),
            }
            wf = self._make_workflow(nodes)
            event = {"type": "test", "data": {"repo": str(repo_dir)}}
            run = WorkflowRun.create("test", event)
            engine = WorkflowEngine(wf, run)
            engine.execute()

        assert run.nodes["step"].outputs["stdout"] == "DOCS"

    def test_no_repo_scope_when_no_event_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        mock_config = MagicMock()
        mock_config.repos = []
        mock_config.slack_bot_token = ""
        mock_config.slack_dm_channel = ""

        with patch("modastack.config.GlobalConfig.load", return_value=mock_config):
            nodes = {
                "step": NodeDef(id="step", type=NodeType.BASH,
                               command="echo ${{repo.content_dir}}"),
            }
            wf = self._make_workflow(nodes)
            event = {"type": "test", "data": {}}
            run = WorkflowRun.create("test", event)
            engine = WorkflowEngine(wf, run)
            engine.execute()

        # Without repo scope, the variable resolves to empty string
        assert run.nodes["step"].outputs["stdout"] == ""




# === Dispatcher Tests ===

class TestWorkflowDispatcher:
    def test_loads_workflows(self):
        d = WorkflowDispatcher()
        d.load_workflows(Path(__file__).parent.parent / "workflows")
        assert len(d.workflows) >= 1
        names = {wf.name for wf, _ in d.workflows}
        assert "issue-lifecycle" in names

    def test_dispatch_matching_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)
        monkeypatch.setattr("modastack.workflow.triggers.WORKFLOWS_DIR",
                           Path(__file__).parent.parent / "workflows")

        d = WorkflowDispatcher()
        d.load_workflows()

        event = {
            "type": "task.assigned",
            "data": {"issue_id": "99", "title": "Test", "repo": "/tmp/test"},
        }
        result = d.dispatch(event)
        assert result is True
        assert d.was_dispatched(event)

    def test_no_dispatch_for_unmatched(self):
        d = WorkflowDispatcher()
        d.load_workflows(Path(__file__).parent.parent / "workflows")
        event = {"type": "slack.message", "data": {"text": "hello"}}
        assert d.dispatch(event) is False
        assert not d.was_dispatched(event)

    def test_repo_specific_workflow_wins(self, tmp_path, monkeypatch):
        """A repo-specific workflow takes priority over the default."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        # Create a default workflow
        default_dir = tmp_path / "defaults"
        default_dir.mkdir()
        (default_dir / "lifecycle.yaml").write_text(
            "name: default-lifecycle\nversion: 1\n"
            "trigger:\n  event: task.assigned\n"
            "nodes:\n  step:\n    type: bash\n    command: echo default\n"
        )

        # Create a repo-specific workflow
        repo_dir = tmp_path / "myrepo" / ".modastack" / "workflows"
        repo_dir.mkdir(parents=True)
        (repo_dir / "lifecycle.yaml").write_text(
            "name: repo-lifecycle\nversion: 1\n"
            "trigger:\n  event: task.assigned\n"
            "nodes:\n  step:\n    type: bash\n    command: echo repo\n"
        )

        d = WorkflowDispatcher()
        d._load_from(repo_dir, source=str(tmp_path / "myrepo"))
        d._load_from(default_dir, source="default")

        event = {
            "type": "task.assigned",
            "data": {"issue_id": "1", "repo": str(tmp_path / "myrepo")},
        }
        best = d._find_best_workflow(event)
        assert best is not None
        assert best.name == "repo-lifecycle"

    def test_default_used_when_no_repo_match(self, tmp_path):
        """Falls back to default when no repo-specific workflow matches."""
        default_dir = tmp_path / "defaults"
        default_dir.mkdir()
        (default_dir / "lifecycle.yaml").write_text(
            "name: default-lifecycle\nversion: 1\n"
            "trigger:\n  event: task.assigned\n"
            "nodes:\n  step:\n    type: bash\n    command: echo default\n"
        )

        repo_dir = tmp_path / "other-repo" / ".modastack" / "workflows"
        repo_dir.mkdir(parents=True)
        (repo_dir / "lifecycle.yaml").write_text(
            "name: other-lifecycle\nversion: 1\n"
            "trigger:\n  event: task.assigned\n"
            "nodes:\n  step:\n    type: bash\n    command: echo other\n"
        )

        d = WorkflowDispatcher()
        d._load_from(repo_dir, source=str(tmp_path / "other-repo"))
        d._load_from(default_dir, source="default")

        event = {
            "type": "task.assigned",
            "data": {"issue_id": "1", "repo": str(tmp_path / "myrepo")},
        }
        best = d._find_best_workflow(event)
        assert best is not None
        assert best.name == "default-lifecycle"

    def test_repo_matches_slug(self):
        """Slug format 'org/repo' matches path ending in 'repo'."""
        d = WorkflowDispatcher()
        assert d._repo_matches("moda-labs/bettertab", "/home/ubuntu/dev/bettertab")
        assert not d._repo_matches("moda-labs/bettertab", "/home/ubuntu/dev/modastack")


class TestPromptInjectPassesMetadata:
    def test_title_and_repo_passed_to_run_phase(self, tmp_path, monkeypatch):
        """_exec_prompt_inject should pass title and repo from event context."""
        monkeypatch.setattr("modastack.workflow.state.RUNS_DIR", tmp_path)

        nodes = {
            "triage": NodeDef(
                id="triage", type=NodeType.PROMPT,
                session="${{event.issue_id}}", inject="/pickup Issue #42",
                wait_for=WaitForDef(phase="triage_complete"),
            ),
        }
        wf = WorkflowDef(
            name="test", version=1,
            trigger=TriggerDef(event="task.assigned"),
            nodes=nodes,
        )
        event = {
            "type": "task.assigned",
            "data": {
                "issue_id": "42",
                "title": "Fix the login bug",
                "repo": "moda-labs/myrepo",
            },
        }
        run = WorkflowRun.create("test", event)
        engine = WorkflowEngine(wf, run)

        captured = {}
        def mock_run_phase(**kwargs):
            captured.update(kwargs)
            return "42"

        with patch("modastack.subagent.run_phase", mock_run_phase):
            with patch.object(engine, "_resolve_cwd", return_value=str(tmp_path)):
                engine._exec_prompt_inject(nodes["triage"])

        assert captured["title"] == "Fix the login bug"
        assert captured["repo"] == "moda-labs/myrepo"
        assert captured["issue_id"] == "42"
