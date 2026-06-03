"""Sub-agent executor — runs engineer phases as Claude Code sessions.

Each engineer gets a persistent ClaudeSDKClient session tracked in the
registry. Sessions survive restarts and can be resumed, interacted with
from the dashboard, or cancelled.

Two execution modes:
  - run_phase(): fire-and-forget async (legacy, used by old engine)
  - run_phase_blocking(): synchronous, blocks until completion (new executor)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess as sp
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry,
)

InputHandler = Callable[[str, dict[str, Any]], str]

log = logging.getLogger(__name__)

ROLES_DIR = Path(__file__).parent.parent / "roles" / "engineer" / "process"
HANDOFF_DIR = Path.home() / ".modastack" / "handoffs"

PHASE_TIMEOUT = {
    "pickup": 1800,
    "triage": 1800,
    "spec": 3000,
    "implement": 3600,
    "prepare-pr": 1800,
    "feedback": 1200,
}


@dataclass
class AgentResult:
    session_id: str
    issue_id: str
    phase: str
    success: bool
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ""
    final_text: str = ""


@dataclass
class RunningAgent:
    issue_id: str
    phase: str
    session_id: str
    task: asyncio.Task[AgentResult]
    started_at: float = field(default_factory=time.time)
    cwd: str = ""
    client: Any = None


_running: dict[str, RunningAgent] = {}


def _resolve_skill_path(phase: str) -> Path | None:
    skill_dir = ROLES_DIR / phase
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return skill_file
    return None


def _build_prompt(phase: str, issue_id: str, context: str = "", cwd: str = "") -> str:
    skill_path = _resolve_skill_path(phase)
    parts = []
    if skill_path:
        parts.append(
            f"Read and follow the skill file at {skill_path}. "
            f"Execute every step exactly as written."
        )
    parts.append(f"Issue: #{issue_id}")

    # Provide worktree base path so agents know where to create worktrees
    if cwd:
        modastack_root = Path(__file__).parent.parent
        repo_name = Path(cwd).name
        worktree_base = modastack_root / "worktrees" / repo_name
        parts.append(f"Worktree base: {worktree_base}")

    if context:
        parts.append(context)
    parts.append(
        f"After completing this phase, update the handoff file at "
        f"{HANDOFF_DIR / f'{issue_id.lower()}.md'} with your results."
    )
    return "\n\n".join(parts)


def _session_name(issue_id: str, phase: str = "") -> str:
    if phase:
        return f"eng-{issue_id.lower()}-{phase}"
    return f"eng-{issue_id.lower()}"


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------
#
# Engineer processes run out-of-band — their own OS process for `modastack
# spawn`, or a worker thread for workflow phases — so they can't reach the
# manager's in-process event queue directly. They post lifecycle events to the
# bus the same way monitor checks do: over HTTP to the local dashboard's
# /api/event endpoint (modastack.cli._post_event). The started emit is
# fire-and-forget on a daemon thread so a missing or unreachable dashboard
# never blocks or breaks the engineer run. The terminal emit (completed/failed)
# blocks briefly on that thread: it's the last action before the spawn process
# exits, and a daemon thread would otherwise be killed mid-POST at shutdown.


def _summarize_output(text: str, max_lines: int = 6, max_chars: int = 600) -> str:
    """Last few non-empty lines of an agent's final output, for event summaries."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])[:max_chars]


def _emit_lifecycle_event(
    event_type: str, data: dict[str, Any], *, blocking: bool = False,
    timeout: float = 5,
) -> None:
    """POST an engineer lifecycle event to the event bus.

    Runs on a daemon thread and swallows all errors — event delivery is
    best-effort and must never fail the engineer run.

    With ``blocking=True`` the caller waits (up to ``timeout`` seconds) for the
    POST to land before returning. This is required for the *terminal* emit
    (session.completed / session.failed): it fires as the last action before the
    spawn process exits, and a daemon thread is killed at interpreter shutdown
    without finishing its in-flight POST. The bounded join can't hang the
    process — ``_post_event`` carries its own socket timeout.
    """
    payload = {k: v for k, v in data.items() if v not in (None, "")}

    def _send() -> None:
        try:
            from modastack.cli import _post_event
            _post_event(event_type, payload)
        except Exception as e:  # never let event posting surface
            log.debug(f"Lifecycle event {event_type} not posted: {e}")

    t = threading.Thread(target=_send, daemon=True, name="lifecycle-event")
    t.start()
    if blocking:
        t.join(timeout)  # let the POST land before the process exits


def _emit_session_started(
    issue_id: str, repo: str, task: str, session_id: str, phase: str = "",
    requested_by: dict | None = None,
) -> None:
    _emit_lifecycle_event("engineer/session.started", {
        "issue_id": issue_id,
        "repo": repo,
        "task": (task or "")[:500],
        "session_id": session_id,
        "phase": phase,
        "requested_by": requested_by or None,
        "text": f"Engineer started working on {issue_id}",
    })


def _emit_session_finished(
    result: "AgentResult", repo: str, session_id: str, started_at: float,
    requested_by: dict | None = None,
) -> None:
    duration = round(time.time() - started_at, 1)
    # Terminal emit: block so the POST lands before the spawn process exits.
    if result.success:
        summary = _summarize_output(result.final_text)
        _emit_lifecycle_event("engineer/session.completed", {
            "issue_id": result.issue_id,
            "repo": repo,
            "session_id": session_id,
            "phase": result.phase,
            "duration": duration,
            "summary": summary,
            "requested_by": requested_by or None,
            "text": f"Engineer finished {result.issue_id} in {duration:.0f}s",
        }, blocking=True)
    else:
        error = result.error or "unknown error"
        _emit_lifecycle_event("engineer/session.failed", {
            "issue_id": result.issue_id,
            "repo": repo,
            "session_id": session_id,
            "phase": result.phase,
            "duration": duration,
            "error": error,
            "requested_by": requested_by or None,
            "text": f"Engineer failed on {result.issue_id}: {error}",
        }, blocking=True)


# ---------------------------------------------------------------------------
# Blocking execution (new executor path)
# ---------------------------------------------------------------------------


def _make_defer_hook() -> dict:
    """PreToolUse hook that defers AskUserQuestion so we can route it."""
    from claude_agent_sdk import HookMatcher

    async def _defer(input_data, tool_use_id, context):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "defer",
            }
        }

    return {"PreToolUse": [HookMatcher(matcher="AskUserQuestion", hooks=[_defer])]}


async def _run_agent_supervised(
    prompt: str,
    cwd: str,
    issue_id: str,
    phase: str,
    timeout: int,
    on_input_needed: InputHandler | None = None,
    max_budget_usd: float | None = None,
) -> AgentResult:
    """Core agent loop. Blocks until the agent finishes or times out.

    When on_input_needed is provided, AskUserQuestion calls are deferred
    via a PreToolUse hook. The deferred question is routed through the
    callback, and the agent is resumed with the answer.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    name = _session_name(issue_id, phase)
    saved_id = load_session_id(name)
    registry = get_registry()

    hooks = _make_defer_hook() if on_input_needed else None

    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        max_turns=200,
        cli_path=get_cli_path(),
        resume=saved_id or None,
        hooks=hooks,
        skills="all",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"You are an engineer agent working on issue #{issue_id}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
    )

    client = ClaudeSDKClient(options)
    registry.update(name, status="running", phase=phase, session_id=saved_id or "")

    result = AgentResult(
        session_id="", issue_id=issue_id, phase=phase, success=False,
    )

    try:
        connect_prompt = prompt if not saved_id else None
        await client.connect(connect_prompt)
        if saved_id:
            await client.query(prompt)

        while True:
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    if text_parts:
                        joined = "\n".join(text_parts)
                        result.final_text = joined
                        log_activity("response", {
                            "text": joined[:500],
                        }, session=name)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg

            if result_msg is None:
                result.error = "connection lost (no ResultMessage)"
                registry.update(name, status="error")
                return result

            save_session_id(name, result_msg.session_id)
            result.session_id = result_msg.session_id
            result.duration_ms += result_msg.duration_ms
            result.total_cost_usd += result_msg.total_cost_usd or 0.0
            result.num_turns += result_msg.num_turns

            if result_msg.deferred_tool_use and on_input_needed:
                deferred = result_msg.deferred_tool_use
                log.info(f"Agent {issue_id}/{phase} deferred {deferred.name}")
                loop = asyncio.get_running_loop()
                answer = await loop.run_in_executor(
                    None, on_input_needed, deferred.name, deferred.input,
                )
                await client.query(answer)
                continue

            result.success = not result_msg.is_error
            if result_msg.is_error:
                result.error = result_msg.result or "unknown error"
            registry.update(name, status="done", phase=phase,
                            session_id=result_msg.session_id)
            log_activity("stop", {"session_id": result_msg.session_id},
                         session=name)
            return result

    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout}s"
        registry.update(name, status="error")
    except Exception as e:
        result.error = str(e)
        registry.update(name, status="error")
        log.error(f"Sub-agent error for {issue_id}/{phase}: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


def run_phase_blocking(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    title: str = "",
    repo: str = "",
    timeout: int | None = None,
    on_input_needed: InputHandler | None = None,
) -> AgentResult:
    """Run a sub-agent phase, blocking until completion.

    Uses asyncio.run() with a fresh event loop — no shared state, no polling.
    If on_input_needed is provided, AskUserQuestion calls are deferred
    and routed through the callback for manager/human answers.
    """
    prompt = _build_prompt(phase, issue_id, context, cwd=cwd)
    effective_timeout = timeout or PHASE_TIMEOUT.get(phase, 1800)

    name = _session_name(issue_id, phase)
    registry = get_registry()
    registry.register(SessionEntry(
        name=name, session_id="", role="engineer",
        issue_id=issue_id, title=title, phase=phase, repo=repo,
        cwd=cwd, status="starting",
    ))

    started_at = time.time()
    _emit_session_started(issue_id, repo, title or context, name, phase=phase)

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_agent_supervised(
                    prompt, cwd, issue_id, phase, effective_timeout,
                    on_input_needed=on_input_needed,
                    max_budget_usd=None,
                ),
                timeout=effective_timeout,
            )
        )
    except asyncio.TimeoutError:
        registry.update(name, status="error")
        result = AgentResult(
            session_id="", issue_id=issue_id, phase=phase,
            success=False, error=f"timeout after {effective_timeout}s",
        )

    _emit_session_finished(result, repo, name, started_at)
    return result


# Issue references in a freeform task, most specific first. We prefer an
# explicit "issue" keyword over a bare "#5" so an incidental "#3" elsewhere in
# the prompt doesn't win over the real reference.
_ISSUE_REF_PATTERNS = (
    re.compile(r"\bissues?\s*#\s*(\d+)\b", re.IGNORECASE),  # "issue #5", "issue#5"
    re.compile(r"\bissues?\s+(\d+)\b", re.IGNORECASE),       # "Issue 5"
    re.compile(r"#(\d+)\b"),                                  # bare "#5"
)


def _parse_issue_number(task: str) -> str | None:
    """Extract an issue number referenced in a freeform task description.

    Recognizes patterns like "issue #5", "Issue 5", or a bare "#5". Returns the
    number as a string (e.g. "5"), or None when no reference is present.
    """
    if not task:
        return None
    for pattern in _ISSUE_REF_PATTERNS:
        match = pattern.search(task)
        if match:
            return match.group(1)
    return None


def _git_remote_name(path: Path) -> str:
    """Return owner/repo from the origin git remote, or "" if unavailable."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=str(path),
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    if not url:
        return ""
    # Handle SSH (git@github.com:owner/repo.git) and HTTPS URLs.
    if ":" in url and "@" in url:
        path_part = url.split(":")[-1]
    else:
        path_part = "/".join(url.split("/")[-2:])
    return path_part.removesuffix(".git")


def _resolve_repo_name(cwd: str) -> str:
    """Resolve a human GitHub-style repo name (owner/repo) for a working dir.

    Prefers an explicit ``repo:`` field in .modastack.yaml, then the origin git
    remote, and finally falls back to the directory basename so the value is
    never empty.
    """
    path = Path(cwd)
    config_path = path / ".modastack.yaml"
    if config_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(config_path.read_text()) or {}
            repo = raw.get("repo")
            if repo:
                return str(repo)
        except Exception:
            pass
    remote = _git_remote_name(path)
    if remote:
        return remote
    return path.name or cwd


def spawn_adhoc(
    cwd: str,
    task: str,
    timeout: int = 3600,
    name: str | None = None,
    requested_by: dict | None = None,
) -> AgentResult:
    """Spawn a one-off engineer agent with a freeform task prompt.

    `requested_by` carries the originating identity (e.g. the Slack user and
    thread that asked for the work) so completion notices can route back to
    them; it is persisted on the SessionEntry and echoed on lifecycle events.

    When the task references an issue (e.g. "issue #5"), that number is used as
    the issue_id in lifecycle events so the manager can correlate activity back
    to the issue. Otherwise we fall back to a stable auto-generated adhoc id.
    """
    import hashlib
    short_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
    issue_id = name or _parse_issue_number(task) or f"adhoc-{short_hash}"
    repo = _resolve_repo_name(cwd)
    requested_by = requested_by or {}

    registry = get_registry()
    registry.register(SessionEntry(
        name=issue_id, session_id="", role="engineer",
        issue_id=issue_id, title=task[:80], phase="adhoc",
        cwd=cwd, repo=repo, status="starting", requested_by=requested_by,
    ))

    started_at = time.time()
    _emit_session_started(issue_id, repo, task, issue_id, phase="adhoc",
                          requested_by=requested_by)

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_agent_supervised(
                    prompt=task, cwd=cwd, issue_id=issue_id,
                    phase="adhoc", timeout=timeout,
                ),
                timeout=timeout,
            )
        )
    except asyncio.TimeoutError:
        registry.update(issue_id, status="error")
        result = AgentResult(
            session_id="", issue_id=issue_id, phase="adhoc",
            success=False, error=f"timeout after {timeout}s",
        )

    _emit_session_finished(result, repo, issue_id, started_at,
                           requested_by=requested_by)
    return result


def _launch_detached(script: str, args: list[str], log_file: Path) -> None:
    """Launch a detached subprocess that survives parent exit."""
    cmd = [sys.executable, "-c", script, *args]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as lf:
        sp.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)


def spawn_adhoc_background(
    cwd: str,
    task: str,
    timeout: int = 3600,
    name: str | None = None,
    requested_by: dict | None = None,
) -> str:
    """Start an engineer agent as a detached subprocess and return immediately.

    Returns the session name so the caller can reference it. The manager
    learns about completion via engineer/session.completed events on the bus.
    The subprocess survives manager restarts.
    """
    import hashlib
    short_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
    issue_id = name or _parse_issue_number(task) or f"adhoc-{short_hash}"

    args_json = json.dumps({
        "cwd": cwd, "task": task, "timeout": timeout,
        "requested_by": requested_by or {},
    })
    script = (
        "import json, sys; from modastack.subagent import spawn_adhoc; "
        "spawn_adhoc(**json.loads(sys.argv[1]))"
    )
    log_file = Path.home() / ".modastack" / "manager" / "logs" / f"eng-{issue_id}-adhoc.jsonl"
    _launch_detached(script, [args_json], log_file)
    return f"eng-{issue_id}"


def launch_workflow_background(name: str, event: dict) -> str:
    """Start a workflow as a detached subprocess and return immediately.

    The workflow executor runs synchronously in the subprocess (wait=True)
    so node state is persisted. The subprocess survives parent exit.
    """
    event_json = json.dumps(event)
    script = (
        "import json, sys; "
        "from modastack.workflow.triggers import WorkflowDispatcher; "
        "d = WorkflowDispatcher(); d.load_all_workflows(); "
        "r = d.run_by_name(sys.argv[1], json.loads(sys.argv[2]), wait=True); "
        "print(f'{sys.argv[1]} {r.status}')"
    )
    issue_id = event.get("data", {}).get("issue_id", "unknown")
    log_file = Path.home() / ".modastack" / "workflow" / "logs" / f"{name}-{issue_id}.log"
    _launch_detached(script, [name, event_json], log_file)
    return f"wf-{name}-{issue_id}"


# ---------------------------------------------------------------------------
# Non-interactive check execution (background monitor path)
# ---------------------------------------------------------------------------

CHECK_TIMEOUT = 600  # monitor checks are short-lived


@dataclass
class CheckResult:
    """Outcome of a non-interactive check agent.

    `finding` is True when the check determined a condition needs attention;
    `summary`/`details` describe it. `success` is False only when the agent
    itself errored or its output couldn't be parsed.
    """

    success: bool
    finding: bool = False
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    error: str = ""
    duration_ms: int = 0
    total_cost_usd: float = 0.0


def _build_check_prompt(description: str, extra: dict[str, Any] | None = None) -> str:
    """Constrained, read-only prompt for a one-shot monitoring check."""
    parts = [
        "You are a non-interactive monitoring check running out-of-band — not "
        "in a conversation. Perform exactly the check described below and "
        "nothing else. You may run read-only shell commands and API calls "
        "(e.g. `gh`, `curl`) to observe the current state. Do NOT modify "
        "files, open or comment on PRs, push commits, or take any corrective "
        "action — only observe and report.",
        f"Check to perform:\n{description}",
    ]
    if extra:
        rendered = "\n".join(f"  {k}: {v}" for k, v in extra.items())
        parts.append(f"Context:\n{rendered}")
    parts.append(
        "When finished, output your result as a SINGLE line of JSON as the very "
        "last thing you say, with nothing after it, in exactly one of these "
        "forms:\n"
        '  {"finding": true, "summary": "<one-line description of what needs '
        'attention>", "details": {<optional structured fields>}}\n'
        '  {"finding": false}\n'
        "Use finding=false when everything is healthy and nothing needs attention."
    )
    return "\n\n".join(parts)


def _extract_json_objects(text: str) -> list[str]:
    """Return top-level brace-balanced JSON object substrings, in order.

    Tracks brace depth while respecting string literals, so nested objects
    (e.g. a "details" sub-object) are kept inside their parent rather than
    split apart.
    """
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start:i + 1])
                start = None
    return objects


def _parse_check_output(text: str) -> tuple[bool, str, dict]:
    """Extract the trailing JSON verdict from a check agent's final message.

    Returns (finding, summary, details). Falls back to finding=False when no
    parseable verdict object is present.
    """
    if not text:
        return False, "", {}
    # Prefer the last parseable object that actually looks like a verdict.
    for chunk in reversed(_extract_json_objects(text)):
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "finding" in parsed:
            finding = bool(parsed.get("finding"))
            summary = str(parsed.get("summary", "")) if finding else ""
            details = parsed.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            return finding, summary, details
    return False, "", {}


def run_check_blocking(
    description: str,
    cwd: str,
    name: str | None = None,
    extra: dict[str, Any] | None = None,
    timeout: int = CHECK_TIMEOUT,
) -> CheckResult:
    """Run a one-shot, non-interactive check agent and parse its verdict.

    Reuses the same supervised agent loop as engineer phases, but with a
    constrained read-only prompt and no input handler. Blocks until the
    agent finishes or times out.
    """
    import hashlib

    short_hash = hashlib.sha256(description.encode()).hexdigest()[:8]
    slug = name or f"check-{short_hash}"
    issue_id = slug
    phase = "check"
    session = _session_name(issue_id, phase)

    prompt = _build_check_prompt(description, extra)

    registry = get_registry()
    registry.register(SessionEntry(
        name=session, session_id="", role="monitor",
        issue_id=issue_id, title=description[:80], phase=phase,
        cwd=cwd, status="starting",
    ))

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_agent_supervised(prompt, cwd, issue_id, phase, timeout),
                timeout=timeout,
            )
        )
    except asyncio.TimeoutError:
        registry.update(session, status="error")
        return CheckResult(success=False, error=f"timeout after {timeout}s")

    if not result.success:
        return CheckResult(
            success=False, error=result.error or "check agent failed",
            raw_output=result.final_text, duration_ms=result.duration_ms,
            total_cost_usd=result.total_cost_usd,
        )

    finding, summary, details = _parse_check_output(result.final_text)
    return CheckResult(
        success=True, finding=finding, summary=summary, details=details,
        raw_output=result.final_text, duration_ms=result.duration_ms,
        total_cost_usd=result.total_cost_usd,
    )


# ---------------------------------------------------------------------------
# Fire-and-forget execution (legacy engine path)
# ---------------------------------------------------------------------------


async def _run_agent(
    prompt: str,
    cwd: str,
    issue_id: str,
    phase: str,
    timeout: int,
    max_budget_usd: float | None = None,
) -> AgentResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    name = _session_name(issue_id, phase)
    saved_id = load_session_id(name)
    registry = get_registry()

    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        max_turns=200,
        cli_path=get_cli_path(),
        resume=saved_id or None,
        skills="all",
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"You are an engineer agent working on issue #{issue_id}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
    )

    client = ClaudeSDKClient(options)
    key = issue_id.lower()
    if key in _running:
        _running[key].client = client

    registry.update(name, status="running", phase=phase, session_id=saved_id or "")

    result = AgentResult(
        session_id="", issue_id=issue_id, phase=phase, success=False,
    )

    try:
        connect_prompt = prompt if not saved_id else None
        await client.connect(connect_prompt)

        if saved_id:
            await client.query(prompt)

        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if text_parts:
                    log_activity("response", {
                        "text": "\n".join(text_parts)[:500],
                    }, session=name)

            elif isinstance(msg, ResultMessage):
                save_session_id(name, msg.session_id)
                result.session_id = msg.session_id
                result.success = not msg.is_error
                result.duration_ms = msg.duration_ms
                result.total_cost_usd = msg.total_cost_usd or 0.0
                result.num_turns = msg.num_turns
                if msg.is_error:
                    result.error = msg.result or "unknown error"
                registry.update(name, status="done", phase=phase, session_id=msg.session_id)
                log_activity("Stop", {
                    "session_id": msg.session_id,
                }, session=name)
    except asyncio.TimeoutError:
        result.error = f"timeout after {timeout}s"
        registry.update(name, status="error")
    except Exception as e:
        result.error = str(e)
        registry.update(name, status="error")
        log.error(f"Sub-agent error for {issue_id}/{phase}: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


def run_phase(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    max_budget_usd: float | None = None,
    title: str = "",
    repo: str = "",
) -> str:
    key = issue_id.lower()
    if key in _running:
        if not _running[key].task.done():
            log.warning(f"Agent already running for {key}, skipping")
            return key
        del _running[key]

    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)

    name = _session_name(issue_id, phase)
    registry = get_registry()
    registry.register(SessionEntry(
        name=name, session_id="", role="engineer",
        issue_id=issue_id, title=title, phase=phase, repo=repo,
        cwd=cwd, status="starting",
    ))

    loop = _ensure_loop()

    started_at = time.time()
    _emit_session_started(issue_id, repo, title or context, name, phase=phase)

    async def _wrapped():
        try:
            result = await asyncio.wait_for(
                _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            registry.update(name, status="error")
            result = AgentResult(
                session_id="",
                issue_id=issue_id,
                phase=phase,
                success=False,
                error=f"timeout after {timeout}s",
            )
        _emit_session_finished(result, repo, name, started_at)
        return result

    future = asyncio.run_coroutine_threadsafe(_wrapped(), loop)
    _running[key] = RunningAgent(
        issue_id=issue_id,
        phase=phase,
        session_id="",
        task=future,
        cwd=cwd,
    )
    log.info(f"Sub-agent started: {issue_id}/{phase} in {cwd}")
    return key


def run_phase_sync(
    issue_id: str,
    phase: str,
    cwd: str,
    context: str = "",
    max_budget_usd: float | None = None,
) -> AgentResult:
    prompt = _build_prompt(phase, issue_id, context)
    timeout = PHASE_TIMEOUT.get(phase, 1800)
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(
        asyncio.wait_for(
            _run_agent(prompt, cwd, issue_id, phase, timeout, max_budget_usd),
            timeout=timeout,
        ),
        loop,
    )
    return future.result(timeout=timeout + 10)


def inject_message(issue_id: str, text: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent or not agent.client:
        log.warning(f"No running agent for {key}")
        return False
    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(agent.client.query(text), loop)
        future.result(timeout=10)
        return True
    except Exception as e:
        log.error(f"Inject to {key} failed: {e}")
        return False


def is_running(issue_id: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return False
    return not agent.task.done()


def get_result(issue_id: str) -> AgentResult | None:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return None
    if not agent.task.done():
        return None
    try:
        result = agent.task.result()
        del _running[key]
        return result
    except Exception as e:
        del _running[key]
        return AgentResult(
            session_id="",
            issue_id=issue_id,
            phase=agent.phase,
            success=False,
            error=str(e),
        )


def cancel_agent(issue_id: str) -> bool:
    key = issue_id.lower()
    agent = _running.get(key)
    if not agent:
        return False
    agent.task.cancel()
    del _running[key]
    name = _session_name(issue_id, agent.phase)
    get_registry().update(name, status="cancelled")
    log.info(f"Sub-agent cancelled: {issue_id}/{agent.phase}")
    return True


def list_agents() -> list[dict[str, Any]]:
    result = []
    for key, agent in list(_running.items()):
        done = agent.task.done()
        elapsed = time.time() - agent.started_at
        result.append({
            "issue_id": agent.issue_id,
            "phase": agent.phase,
            "cwd": agent.cwd,
            "running": not done,
            "elapsed_s": int(elapsed),
        })
    return result


_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return _loop

    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True, name="subagent-loop")
    _loop_thread.start()

    # Wait until the loop is actually running
    while not _loop.is_running():
        time.sleep(0.01)

    return _loop
