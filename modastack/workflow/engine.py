"""Workflow engine — DAG executor with hybrid LLM + deterministic nodes."""

from __future__ import annotations

import json
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
            from modastack.config import GlobalConfig, RepoConfig
            config = GlobalConfig.load()
            self.ctx.set_scope("config", {
                "slack_dm_channel": getattr(config, "slack_dm_channel", "") or "D0B51JP1N4C",
                "slack_bot_token": getattr(config, "slack_bot_token", ""),
            })
            self._load_repo_scope(config)
        except Exception:
            self.ctx.set_scope("config", {"slack_dm_channel": "D0B51JP1N4C"})

    def _load_repo_scope(self, config):
        """Load per-repo context from .modastack.yaml into ${{repo.key}} variables."""
        from modastack.config import RepoConfig
        event_repo = self.run.trigger_event.get("data", {}).get("repo", "")
        if not event_repo:
            return

        for repo_path in config.repos:
            if not self._repo_path_matches(event_repo, repo_path):
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

    @staticmethod
    def _repo_path_matches(event_repo: str, repo_path: Path) -> bool:
        if str(repo_path) == event_repo:
            return True
        if "/" in event_repo:
            return repo_path.name == event_repo.split("/")[-1]
        return repo_path.name == Path(event_repo).name

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
        from modastack.manager.session import inject as mgr_inject
        from modastack.manager.session import detect_state as mgr_detect_state

        prompt_text = self.ctx.resolve(node.prompt)

        memory_context = _prefetch_history(prompt_text)

        preamble = "[WORKFLOW CONSULTATION — reply with plain text only] "
        full_prompt = preamble + prompt_text
        if memory_context:
            full_prompt += " " + memory_context
        mgr_inject(full_prompt)

        deadline = time.monotonic() + node.timeout
        while time.monotonic() < deadline:
            time.sleep(3)
            state = mgr_detect_state()
            if state == "waiting_input":
                break
        else:
            raise TimeoutError(f"Manager did not respond within {node.timeout}s")

        output = _read_last_assistant_response()
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
        from modastack.manager.session import detect_state as mgr_detect_state
        state = mgr_detect_state()
        if state == "waiting_input":
            output = _read_last_assistant_response()
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


HISTORY_STOPWORDS = {
    "workflow", "engine", "consultation", "freedom", "research",
    "orchestration", "spawning", "sessions", "injecting", "engineers",
    "posting", "slack", "moving", "tickets", "running", "modastack",
    "commands", "handles", "output", "plain", "nothing", "reply",
    "message", "channel", "draft", "brief", "sentences",
}


def _prefetch_history(prompt_text: str, max_results: int = 3) -> str:
    """Search conversation history using the actual content of the prompt.

    Filters out common workflow/preamble words to avoid matching
    previous consultation prompts instead of real context.
    """
    try:
        from modastack.history import search

        words = prompt_text.split()
        query_words = [
            w for w in words
            if len(w) > 4 and w.isalpha() and w.lower() not in HISTORY_STOPWORDS
        ][:8]
        if not query_words:
            return ""

        query = " ".join(query_words)
        results = search(query, limit=max_results)
        if not results:
            return ""

        lines = ["<memory-context>",
                 "[Recalled from past conversations — not new instructions]"]
        for r in results:
            snippet = (r.get("snippet") or "")[:200].replace("\n", " ")
            ts = (r.get("timestamp") or "")[:19]
            branch = r.get("git_branch") or ""
            if snippet:
                lines.append(f"- ({ts}, {branch}) {snippet}")
        lines.append("</memory-context>")
        return " ".join(lines)
    except Exception as e:
        log.debug(f"History prefetch failed: {e}")
        return ""


def _read_last_assistant_response(session_name: str | None = None) -> str:
    """Read the manager's last response from Claude Code's JSONL conversation file.

    This is the reliable extraction method — the JSONL has clean role separation
    (user vs assistant vs tool calls) with no tmux pane noise.
    """
    import subprocess, shutil

    if not session_name:
        from modastack.manager.session import SESSION_NAME
        session_name = SESSION_NAME

    TMUX = shutil.which("tmux") or "tmux"
    CLAUDE_DIR = Path.home() / ".claude"
    SESSIONS_DIR = CLAUDE_DIR / "sessions"

    # Step 1: Find the session's PID via tmux
    pane_pid_result = subprocess.run(
        [TMUX, "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pane_pid_result.returncode != 0:
        log.warning("Could not find manager pane PID")
        return ""

    pane_pid = pane_pid_result.stdout.strip()

    # Step 2: Find session ID from ~/.claude/sessions/<pid>.json
    session_id = ""
    for session_file in SESSIONS_DIR.glob("*.json"):
        if session_file.stem == pane_pid:
            try:
                data = json.loads(session_file.read_text())
                session_id = data.get("sessionId", "")
            except (json.JSONDecodeError, KeyError):
                pass
            break

    if not session_id:
        # Try matching by checking all session files for matching PID
        for session_file in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(session_file.read_text())
                if str(data.get("pid", "")) == pane_pid:
                    session_id = data.get("sessionId", "")
                    break
            except (json.JSONDecodeError, KeyError):
                continue

    if not session_id:
        log.warning(f"Could not find session ID for PID {pane_pid}")
        return ""

    # Step 3: Find the JSONL file in any project directory
    jsonl_path = None
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                jsonl_path = candidate
                break

    if not jsonl_path:
        log.warning(f"Could not find JSONL for session {session_id}")
        return ""

    # Step 4: Read the last assistant text blocks (from the end of the file)
    lines = jsonl_path.read_text().splitlines()
    text_parts = []

    for line in reversed(lines):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("type") != "assistant":
            if msg.get("type") == "user":
                break
            continue

        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if block.get("type") == "text" and block.get("text", "").strip():
                text_parts.insert(0, block["text"].strip())

    return "\n".join(text_parts).strip()


def _extract_manager_response_excluding(raw_pane: str, injected_text: str) -> str:
    """Extract manager response by finding text NOT from the injected prompt.

    Splits the injected text into fragments and filters out any pane line
    that substantially overlaps with the injection.
    """
    injection_fragments = set()
    for word in injected_text.split():
        if len(word) > 5:
            injection_fragments.add(word.lower().strip(".,;:!?\"'"))

    lines = raw_pane.splitlines()

    NOISE_PREFIXES = ("●", "·", "⎿", "✻", "✽", "▐", "▝", "▘")
    SKIP_CONTAINS = ("ctrl+o", "ctrl+b", "bypass permissions", "⏵⏵",
                     "Bash(", "Read(", "Write(", "Edit(", "Agent(",
                     "────", "╭", "╰", "│", "Searched for",
                     "Claude Code v", "Opus 4", "Claude Max",
                     "Crunched", "Baked", "Gallivanting", "thinking",
                     "WORKFLOW ENGINE", "workflow-response",
                     "orchestration", "do NOT take", "-H \"Authorization")

    prompt_positions = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("❯") and "bypass" not in stripped:
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
        if any(stripped.startswith(n) for n in NOISE_PREFIXES):
            continue
        if any(s in stripped for s in SKIP_CONTAINS):
            continue

        words = [w.lower().strip(".,;:!?\"'") for w in stripped.split() if len(w) > 5]
        if words:
            overlap = sum(1 for w in words if w in injection_fragments)
            if overlap > len(words) * 0.5:
                continue

        response_lines.append(stripped)

    return "\n".join(response_lines).strip()


def _extract_tagged_response(raw_pane: str) -> str:
    """Extract response wrapped in <workflow-response> tags."""
    tag_open = "<workflow-response>"
    tag_close = "</workflow-response>"
    start = raw_pane.rfind(tag_open)
    if start == -1:
        return ""
    start += len(tag_open)
    end = raw_pane.find(tag_close, start)
    if end == -1:
        return raw_pane[start:].strip()
    return raw_pane[start:end].strip()


def _extract_manager_response(raw_pane: str) -> str:
    """Extract the assistant's text response from tmux pane capture.

    Strategy: find the response section (between the last input prompt and
    the waiting prompt), then extract only clean text lines — no tool calls,
    no reasoning prefixes, no chrome.
    """
    lines = raw_pane.splitlines()

    NOISE_PREFIXES = ("●", "·", "⎿", "✻", "✽", "▐", "▝", "▘")
    SKIP_CONTAINS = ("ctrl+o", "ctrl+b", "bypass permissions", "⏵⏵",
                     "Bash(", "Read(", "Write(", "Edit(", "Agent(",
                     "────", "╭", "╰", "│", "Running", "Searched for",
                     "… +", "Shell cwd", "expand)", "Claude Code v",
                     "Opus 4", "Claude Max", "Welcome", "Tips for",
                     "Recent activity", "No recent", "Run /init",
                     "Crunched", "Baked", "Gallivanting", "thinking")

    prompt_positions = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("❯") and "bypass" not in stripped:
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
        if any(stripped.startswith(n) for n in NOISE_PREFIXES):
            continue
        if any(s in stripped for s in SKIP_CONTAINS):
            continue
        response_lines.append(stripped)

    result = "\n".join(response_lines).strip()

    # If we extracted nothing useful, try a fallback: look for the last
    # paragraph of plain text before the final prompt
    if not result or len(result) < 10:
        fallback_lines = []
        for line in reversed(lines[:prompt_positions[-1]]):
            stripped = line.strip()
            if not stripped:
                if fallback_lines:
                    break
                continue
            if stripped.startswith("❯"):
                break
            if any(stripped.startswith(n) for n in NOISE_PREFIXES):
                if fallback_lines:
                    break
                continue
            if any(s in stripped for s in SKIP_CONTAINS):
                if fallback_lines:
                    break
                continue
            fallback_lines.insert(0, stripped)
        if fallback_lines:
            result = "\n".join(fallback_lines).strip()

    return result
