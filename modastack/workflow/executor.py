"""Workflow executor — synchronous, blocking, resumable.

Walks a workflow DAG in topological order. Each node runs to completion
before the next starts. Sub-agent phases block until the agent finishes.
State is persisted after every node transition so a killed executor can
resume from the last completed node.

Three possible outcomes:
  - completed: all nodes finished (some may be skipped)
  - failed: a node failed and downstream nodes couldn't run
  - suspended: an approval node needs an external event
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Callable

import yaml as _yaml

from .actions import ActionRegistry, build_registry
from .schema import BranchDef, NodeDef, NodeType, WorkflowDef
from .state import NodeState, WorkflowRun
from .variables import VariableContext

log = logging.getLogger(__name__)

HANDOFF_DIR_DEFAULT = "~/.modastack/handoffs"


class ExecutorResult:
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"


InputHandler = Callable[[str, dict[str, Any]], str]
NotifyFn = Callable[[str], None]


class WorkflowExecutor:
    """Synchronous, blocking workflow executor.

    Key properties:
    - Each node runs to completion before the next starts
    - Sub-agents block via run_phase_blocking (no polling)
    - State persisted after every transition (crash-resumable)
    - Approval nodes suspend the executor for external events
    """

    def __init__(
        self,
        workflow: WorkflowDef,
        run: WorkflowRun,
        registry: ActionRegistry | None = None,
        on_notify: NotifyFn | None = None,
        on_input_needed: InputHandler | None = None,
    ):
        self.workflow = workflow
        self.run = run
        self.registry = registry or build_registry()
        self.on_notify = on_notify or (lambda msg: None)
        self.on_input_needed = on_input_needed
        self.ctx = VariableContext()
        self._pending_events: list[dict] = []

        self.ctx.set_scope("event", run.trigger_event.get("data", {}))
        self._load_config_scope()

        for node_id, ns in run.nodes.items():
            if ns.status == "completed":
                self.ctx.set_scope(node_id, ns.outputs)

    def execute(self) -> str:
        """Execute the workflow, blocking until done.

        Returns ExecutorResult.COMPLETED, FAILED, or SUSPENDED.
        """
        order = self.workflow.topological_order()
        log.info(f"Executor '{self.workflow.name}' run {self.run.run_id}: "
                 f"executing {len(order)} nodes")

        for node_id in order:
            node = self.workflow.nodes[node_id]
            ns = self.run.node_state(node_id)

            if ns.status in ("completed", "skipped", "failed"):
                continue

            if not self._deps_satisfied(node):
                continue

            # Approval nodes waiting for external event
            if ns.status == "waiting" and node.type == NodeType.APPROVAL:
                result = self._check_approval(node)
                if result is None:
                    return ExecutorResult.SUSPENDED
                self._complete_node(node_id, ns, result)
                continue

            # Check when condition
            if node.when:
                try:
                    if not self.ctx.evaluate_condition(node.when):
                        ns.status = "skipped"
                        log.info(f"  {node_id}: skipped (condition false)")
                        self.run.save()
                        continue
                except Exception as e:
                    log.warning(f"  {node_id}: condition eval error: {e}")
                    ns.status = "skipped"
                    self.run.save()
                    continue

            # Approval node (fresh)
            if node.type == NodeType.APPROVAL:
                result = self._check_approval(node)
                if result is None:
                    ns.status = "waiting"
                    self.run.save()
                    log.info(f"  {node_id}: suspended (waiting for approval)")
                    return ExecutorResult.SUSPENDED
                self._complete_node(node_id, ns, result)
                continue

            # All other nodes: mark running, execute, mark done/failed
            ns.status = "running"
            ns.started_at = _now()
            self.run.save()
            log.info(f"  {node_id}: started ({node.type.value})")

            try:
                outputs = self._run_node(node)
                self._complete_node(node_id, ns, outputs)
            except Exception as e:
                ns.status = "failed"
                ns.error = str(e)
                ns.completed_at = _now()
                self.run.save()
                log.error(f"  {node_id}: failed — {e}")

        return self._finalize(order)

    def feed_event(self, event: dict):
        """Feed an external event (for approval nodes)."""
        self._pending_events.append(event)

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize(self, order: list[str]) -> str:
        """Determine final workflow status after one pass through the DAG."""
        statuses = {nid: self.run.node_state(nid).status for nid in order}

        waiting = [nid for nid, s in statuses.items() if s == "waiting"]
        if waiting:
            return ExecutorResult.SUSPENDED

        # Mark unreachable pending nodes as skipped
        for nid in order:
            if statuses[nid] == "pending":
                node = self.workflow.nodes[nid]
                if not self._deps_could_ever_satisfy(node):
                    self.run.node_state(nid).status = "skipped"
                    statuses[nid] = "skipped"

        failed = [nid for nid, s in statuses.items() if s == "failed"]
        self.run.status = "failed" if failed else "completed"
        self.run.completed_at = _now()
        self.run.save()

        issue_id = self.run.trigger_event.get("data", {}).get("issue_id", "")
        title = self.run.trigger_event.get("data", {}).get("title", "")

        if self.run.status == "completed":
            self.on_notify(
                f"Workflow complete for issue #{issue_id} ({title}). "
                f"All phases finished successfully."
            )
        else:
            self.on_notify(
                f"Workflow failed for issue #{issue_id} ({title}). "
                f"Failed nodes: {', '.join(failed)}. "
                f"Review and decide: retry, fix manually, or close."
            )

        log.info(f"Executor '{self.workflow.name}' run {self.run.run_id}: "
                 f"{self.run.status}")
        return self.run.status

    # ------------------------------------------------------------------
    # Node execution
    # ------------------------------------------------------------------

    def _complete_node(self, node_id: str, ns: NodeState, outputs: dict):
        ns.status = "completed"
        ns.outputs = outputs
        ns.completed_at = _now()
        self.ctx.set_scope(node_id, outputs)
        self.run.save()
        log.info(f"  {node_id}: completed")

    def _run_node(self, node: NodeDef) -> dict:
        if node.type == NodeType.BASH:
            return self._run_bash(node)
        elif node.type == NodeType.ACTION:
            return self._run_action(node)
        elif node.type == NodeType.PROMPT:
            return self._run_prompt(node)
        elif node.type == NodeType.MANAGER:
            return self._run_manager(node)
        elif node.type == NodeType.GATE:
            return self._run_gate(node)
        raise ValueError(f"Unknown node type: {node.type}")

    def _run_bash(self, node: NodeDef) -> dict:
        command = self.ctx.resolve(node.command)
        cwd = self.ctx.resolve(node.cwd) if node.cwd else None
        env = {k: self.ctx.resolve(v) for k, v in node.env.items()} if node.env else None

        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=node.timeout, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Bash failed (rc={result.returncode}): "
                f"{result.stderr.strip()[:500]}"
            )
        return {"stdout": result.stdout.strip(), "returncode": 0}

    def _run_action(self, node: NodeDef) -> dict:
        resolved = {}
        for k, v in node.params.items():
            resolved[k] = self.ctx.resolve(str(v)) if isinstance(v, str) else v
        return self.registry.execute(node.action, resolved)

    def _run_prompt(self, node: NodeDef) -> dict:
        """Launch a sub-agent and BLOCK until it finishes."""
        from modastack.subagent import run_phase_blocking
        from pathlib import Path

        issue_id = self.ctx.resolve(node.session).lstrip("#")
        inject_text = self.ctx.resolve(node.inject)
        phase = self._detect_phase(inject_text)
        cwd = self._resolve_cwd(issue_id)
        title = self.ctx.resolve("${{event.title}}") if "event" in self.ctx.scopes else ""
        repo = self.ctx.resolve("${{event.repo}}") if "event" in self.ctx.scopes else ""

        result = run_phase_blocking(
            issue_id=issue_id,
            phase=phase,
            cwd=cwd,
            context=inject_text,
            title=title,
            repo=repo,
            timeout=node.timeout,
            on_input_needed=self.on_input_needed,
        )

        if not result.success:
            raise RuntimeError(
                f"Sub-agent {issue_id}/{phase} failed: {result.error}"
            )

        outputs = {
            "_agent_completed": True,
            "_agent_cost_usd": result.total_cost_usd,
            "_agent_duration_ms": result.duration_ms,
        }

        handoff = self._read_handoff(issue_id)
        if handoff:
            self.ctx.set_scope("handoff", handoff)
            for key, expr in node.outputs.items():
                outputs[key] = self.ctx.resolve(expr)

        return outputs

    def _run_manager(self, node: NodeDef) -> dict:
        from modastack.manager.session import (
            inject_capture as mgr_inject_capture,
            last_inject_error,
        )

        prompt_text = self.ctx.resolve(node.prompt)
        preamble = "[WORKFLOW CONSULTATION — reply with plain text only] "
        full_prompt = preamble + prompt_text

        # The manager session is shared with the event drain loop and other
        # workflow runs, so it is frequently mid-turn when we need it. Wait
        # for it to free up (up to the node timeout) instead of failing the
        # instant it is busy. inject_capture() blocks until the manager
        # finishes its turn and returns this turn's reply atomically, so a
        # concurrent inject can't substitute its own response.
        ok, output = mgr_inject_capture(full_prompt, timeout=node.timeout,
                                        wait_for_ready=node.timeout)
        if not ok:
            raise RuntimeError(
                f"Failed to inject into manager session: {last_inject_error()}"
            )

        return {"output": output or ""}

    def _run_gate(self, node: NodeDef) -> dict:
        for branch_name, branch_def in node.branches.items():
            try:
                if self.ctx.evaluate_condition(branch_def.when):
                    return {"branch": branch_name, "goto": branch_def.goto}
            except Exception:
                continue

        if node.fallback:
            fb = node.branches.get(node.fallback, BranchDef())
            return {"branch": node.fallback, "goto": fb.goto}

        raise RuntimeError(f"Gate '{node.id}': no branch matched and no fallback")

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    def _check_approval(self, node: NodeDef) -> dict | None:
        if not node.listen_for:
            return None

        match_text = self.ctx.resolve(node.listen_for.match).lower()
        source = node.listen_for.source
        channel = (self.ctx.resolve(node.listen_for.channel_id)
                   if node.listen_for.channel_id else "")

        for i, event in enumerate(self._pending_events):
            if source and event.get("source") != source:
                continue
            if channel:
                if event.get("data", {}).get("channel_id", "") != channel:
                    continue
            text = event.get("data", {}).get("text", "").lower()
            if match_text in text:
                self._pending_events.pop(i)
                return {"approved": True, "text": text}

        return None

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def _deps_satisfied(self, node: NodeDef) -> bool:
        for dep_id in node.depends_on:
            ns = self.run.nodes.get(dep_id)
            if not ns or ns.status not in ("completed", "skipped"):
                return False
        return True

    def _deps_could_ever_satisfy(self, node: NodeDef) -> bool:
        for dep_id in node.depends_on:
            ns = self.run.nodes.get(dep_id)
            if ns and ns.status == "failed":
                return False
        return True

    # ------------------------------------------------------------------
    # Helpers (shared with old engine)
    # ------------------------------------------------------------------

    def _detect_phase(self, inject_text: str) -> str:
        text_lower = inject_text.lower()
        for phase in ("pickup", "spec", "implement", "prepare-pr", "feedback"):
            if f"/{phase}" in text_lower:
                return phase
        return "implement"

    def _resolve_cwd(self, issue_id: str) -> str:
        event_repo = self.run.trigger_event.get("data", {}).get("repo", "")
        if not event_repo:
            from pathlib import Path
            return str(Path.home())

        from modastack.config import GlobalConfig
        config = GlobalConfig.load()
        repo_name = event_repo.split("/")[-1] if "/" in event_repo else event_repo
        for p in config.repos:
            if p.name == repo_name:
                return str(p)
        return event_repo

    def _read_handoff(self, issue_id: str) -> dict:
        from pathlib import Path
        from modastack.config import GlobalConfig

        config = GlobalConfig.load()
        handoff_dir = Path.home() / ".modastack" / "handoffs"
        iid_lower = issue_id.lstrip("#").lower()
        modastack_root = Path(__file__).parent.parent

        for repo_path in config.repos:
            repo_name = repo_path.name
            for candidate in [
                # New location: modastack/worktrees/<repo>/<issue>/
                modastack_root / "worktrees" / repo_name / iid_lower / ".modastack" / "handoff.md",
                # Legacy location: <repo>/worktrees/<issue>/
                repo_path / "worktrees" / iid_lower / ".modastack" / "handoff.md",
                handoff_dir / f"{iid_lower}.md",
                handoff_dir / f"{issue_id}.md",
            ]:
                if candidate.exists():
                    try:
                        content = candidate.read_text()
                        if content.startswith("---"):
                            end = content.index("---", 3)
                            return _yaml.safe_load(content[3:end]) or {}
                    except Exception:
                        continue
        return {}

    def _load_config_scope(self):
        try:
            from modastack.config import GlobalConfig, RepoConfig
            config = GlobalConfig.load()
            self.ctx.set_scope("config", {
                "slack_dm_channel": getattr(config, "slack_dm_channel", "") or "",
                "slack_bot_token": getattr(config, "slack_bot_token", ""),
            })
            self._load_repo_scope(config)
        except Exception:
            self.ctx.set_scope("config", {})

    def _load_repo_scope(self, config):
        from modastack.config import RepoConfig
        from pathlib import Path

        event_repo = self.run.trigger_event.get("data", {}).get("repo", "")
        if not event_repo:
            return

        for repo_path in config.repos:
            if not _repo_path_matches(event_repo, repo_path):
                continue
            try:
                repo_config = RepoConfig.from_file(repo_path)
                scope = {
                    "path": str(repo_config.path),
                    "task_tracking": repo_config.task_tracking,
                    "project": repo_config.project,
                    "test_command": repo_config.test_command,
                    **repo_config.context,
                }
                self.ctx.set_scope("repo", scope)
                return
            except FileNotFoundError:
                continue


def _repo_path_matches(event_repo: str, repo_path) -> bool:
    from pathlib import Path
    if str(repo_path) == event_repo:
        return True
    if "/" in event_repo:
        return repo_path.name == event_repo.split("/")[-1]
    return repo_path.name == Path(event_repo).name


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
