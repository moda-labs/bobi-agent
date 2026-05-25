"""Workflow engine — DAG executor with hybrid LLM + deterministic nodes."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml as _yaml

from .actions import ActionRegistry, build_registry
from .schema import NodeDef, NodeType, WorkflowDef
from .state import NodeState, WorkflowRun
from .variables import VariableContext

log = logging.getLogger(__name__)

HANDOFF_DIR = Path.home() / ".modastack" / "handoffs"


class WaitingError(Exception):
    pass


class WorkflowEngine:

    def __init__(self, workflow: WorkflowDef, run: WorkflowRun,
                 registry: ActionRegistry | None = None):
        self.workflow = workflow
        self.run = run
        self.registry = registry or build_registry()
        self.ctx = VariableContext()
        self._pending_approval_events: list[dict] = []

        self.ctx.set_scope("event", run.trigger_event.get("data", {}))
        self._load_config_scope()

        for node_id, ns in run.nodes.items():
            if ns.status == "completed":
                self.ctx.set_scope(node_id, ns.outputs)

    def _load_config_scope(self):
        try:
            from modastack.config import GlobalConfig
            config = GlobalConfig.load()
            self.ctx.set_scope("config", {
                "slack_dm_channel": getattr(config, "slack_dm_channel", ""),
                "slack_bot_token": getattr(config, "slack_bot_token", ""),
            })
        except Exception:
            self.ctx.set_scope("config", {})

    def execute(self):
        order = self.workflow.topological_order()
        log.info(f"Workflow '{self.workflow.name}' run {self.run.run_id}: "
                 f"executing {len(order)} nodes")

        while True:
            progress = False
            all_terminal = True

            for node_id in order:
                node_def = self.workflow.nodes[node_id]
                ns = self.run.node_state(node_id)

                if ns.status in ("completed", "skipped", "failed"):
                    continue

                all_terminal = False

                if ns.status == "pending":
                    if not self._deps_satisfied(node_def):
                        continue

                    if node_def.when:
                        try:
                            if not self.ctx.evaluate_condition(node_def.when):
                                ns.status = "skipped"
                                log.info(f"  {node_id}: skipped (condition false)")
                                self.run.save()
                                progress = True
                                continue
                        except Exception as e:
                            log.warning(f"  {node_id}: condition eval error: {e}, skipping")
                            ns.status = "skipped"
                            self.run.save()
                            progress = True
                            continue

                    ns.status = "running"
                    ns.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    self.run.save()
                    log.info(f"  {node_id}: started ({node_def.type.value})")
                    progress = True

                    try:
                        outputs = self._execute_node(node_def)
                        ns.status = "completed"
                        ns.outputs = outputs
                        ns.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                        self.ctx.set_scope(node_id, outputs)
                        log.info(f"  {node_id}: completed")
                    except WaitingError:
                        ns.status = "waiting"
                        log.info(f"  {node_id}: waiting")
                    except Exception as e:
                        ns.status = "failed"
                        ns.error = str(e)
                        log.error(f"  {node_id}: failed — {e}")

                    self.run.save()

                elif ns.status in ("running", "waiting"):
                    try:
                        outputs = self._poll_node(node_def, ns)
                        if outputs is not None:
                            ns.status = "completed"
                            ns.outputs = outputs
                            ns.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                            self.ctx.set_scope(node_id, outputs)
                            log.info(f"  {node_id}: completed (after poll)")
                            self.run.save()
                            progress = True
                    except TimeoutError:
                        ns.status = "failed"
                        ns.error = "timeout"
                        log.error(f"  {node_id}: timed out")
                        self.run.save()

            if all_terminal:
                failed = [nid for nid, ns in self.run.nodes.items()
                          if ns.status == "failed"]
                self.run.status = "failed" if failed else "completed"
                self.run.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.run.save()
                log.info(f"Workflow '{self.workflow.name}' run {self.run.run_id}: "
                         f"{self.run.status}")
                break

            if not progress:
                time.sleep(5)

    def feed_event(self, event: dict):
        self._pending_approval_events.append(event)

    def _deps_satisfied(self, node: NodeDef) -> bool:
        for dep_id in node.depends_on:
            ns = self.run.nodes.get(dep_id)
            if not ns or ns.status not in ("completed", "skipped"):
                return False
        return True

    def _execute_node(self, node: NodeDef) -> dict:
        if node.type == NodeType.BASH:
            return self._exec_bash(node)
        elif node.type == NodeType.ACTION:
            return self._exec_action(node)
        elif node.type == NodeType.PROMPT:
            self._exec_prompt_inject(node)
            raise WaitingError()
        elif node.type == NodeType.MANAGER:
            return self._exec_manager(node)
        elif node.type == NodeType.APPROVAL:
            raise WaitingError()
        elif node.type == NodeType.GATE:
            return self._exec_gate(node)
        raise ValueError(f"Unknown node type: {node.type}")

    def _exec_bash(self, node: NodeDef) -> dict:
        command = self.ctx.resolve(node.command)
        cwd = self.ctx.resolve(node.cwd) if node.cwd else None
        env_resolved = {k: self.ctx.resolve(v) for k, v in node.env.items()} if node.env else None

        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=node.timeout, env=env_resolved,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Bash failed (rc={result.returncode}): "
                             f"{result.stderr.strip()[:500]}")

        return {"stdout": result.stdout.strip(), "returncode": 0}

    def _exec_action(self, node: NodeDef) -> dict:
        resolved_params = {}
        for k, v in node.params.items():
            resolved_params[k] = self.ctx.resolve(str(v)) if isinstance(v, str) else v
        return self.registry.execute(node.action, resolved_params)

    def _exec_prompt_inject(self, node: NodeDef) -> None:
        from modastack.session import inject, session_exists

        session_id = self.ctx.resolve(node.session).lstrip("#")
        text = self.ctx.resolve(node.inject)

        if not session_exists(session_id):
            raise RuntimeError(f"Engineer session '{session_id}' not found")

        inject(session_id, text)

    def _exec_manager(self, node: NodeDef) -> dict:
        from manager.session import inject as mgr_inject, capture as mgr_capture
        from manager.session import detect_state as mgr_detect_state

        prompt_text = self.ctx.resolve(node.prompt)
        mgr_inject(prompt_text)

        deadline = time.monotonic() + node.timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            state = mgr_detect_state()
            if state == "waiting_input":
                break
        else:
            raise TimeoutError(f"Manager did not respond within {node.timeout}s")

        raw = mgr_capture(lines=100)
        output = _extract_manager_response(raw)
        return {"output": output}

    def _exec_gate(self, node: NodeDef) -> dict:
        for branch_name, branch_def in node.branches.items():
            try:
                if self.ctx.evaluate_condition(branch_def.when):
                    return {"branch": branch_name, "goto": branch_def.goto}
            except Exception:
                continue

        if node.fallback:
            from .schema import BranchDef
            return {"branch": node.fallback, "goto": node.branches.get(node.fallback, BranchDef()).goto}

        raise RuntimeError(f"Gate '{node.id}': no branch matched and no fallback")

    def _poll_node(self, node: NodeDef, ns: NodeState) -> dict | None:
        if ns.started_at:
            try:
                started = time.mktime(time.strptime(ns.started_at, "%Y-%m-%dT%H:%M:%S"))
                if time.time() - started > node.timeout:
                    raise TimeoutError()
            except ValueError:
                pass

        if node.type == NodeType.PROMPT:
            return self._poll_prompt(node)
        elif node.type == NodeType.APPROVAL:
            return self._poll_approval(node)
        elif node.type == NodeType.MANAGER:
            return self._poll_manager(node)
        return None

    def _poll_prompt(self, node: NodeDef) -> dict | None:
        if not node.wait_for or not node.wait_for.phase:
            return None

        session_id = self.ctx.resolve(node.session)
        handoff = self._read_handoff(session_id)

        if handoff.get("phase") == node.wait_for.phase:
            self.ctx.set_scope("handoff", handoff)
            outputs = {}
            for key, expr in node.outputs.items():
                outputs[key] = self.ctx.resolve(expr)
            return outputs

        return None

    def _poll_approval(self, node: NodeDef) -> dict | None:
        if not node.listen_for:
            return None

        match_text = self.ctx.resolve(node.listen_for.match).lower()
        source = node.listen_for.source
        channel = self.ctx.resolve(node.listen_for.channel_id) if node.listen_for.channel_id else ""

        consumed = []
        for i, event in enumerate(self._pending_approval_events):
            if source and event.get("source") != source:
                continue
            if channel:
                event_channel = event.get("data", {}).get("channel_id", "")
                if event_channel != channel:
                    continue
            text = event.get("data", {}).get("text", "").lower()
            if match_text in text:
                consumed.append(i)
                for idx in reversed(consumed):
                    self._pending_approval_events.pop(idx)
                return {"approved": True, "text": text}

        return None

    def _poll_manager(self, node: NodeDef) -> dict | None:
        from manager.session import detect_state as mgr_detect_state, capture as mgr_capture
        state = mgr_detect_state()
        if state == "waiting_input":
            raw = mgr_capture(lines=100)
            output = _extract_manager_response(raw)
            return {"output": output}
        return None

    def _read_handoff(self, issue_id: str) -> dict:
        from modastack.config import GlobalConfig
        config = GlobalConfig.load()
        iid_lower = issue_id.lstrip("#").lower()

        for repo_path in config.repos:
            for candidate in [
                repo_path / "worktrees" / iid_lower / ".modastack" / "handoff.md",
                Path.home() / ".modastack" / "handoffs" / f"{iid_lower}.md",
                Path.home() / ".modastack" / "handoffs" / f"{issue_id}.md",
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


def _extract_manager_response(raw_pane: str) -> str:
    """Extract the assistant's text response from tmux pane capture.

    Finds content between the last two ❯ prompts, filtering out
    tool call noise and UI chrome.
    """
    lines = raw_pane.splitlines()

    NOISE = ("●", "·", "⎿", "✻", "✽")
    SKIP_CONTAINS = ("ctrl+o", "ctrl+b", "bypass permissions", "⏵⏵",
                     "Bash(", "Read(", "Write(", "Edit(", "Agent(", "────")

    prompt_positions = []
    for i, line in enumerate(lines):
        if "❯" in line and "bypass" not in line:
            prompt_positions.append(i)

    if len(prompt_positions) < 2:
        return ""

    start = prompt_positions[-2] + 1
    end = prompt_positions[-1]

    response_lines = []
    for line in lines[start:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(n) for n in NOISE):
            continue
        if any(s in stripped for s in SKIP_CONTAINS):
            continue
        response_lines.append(stripped)

    return "\n".join(response_lines).strip()
