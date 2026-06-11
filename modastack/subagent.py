"""Sub-agent executor — runs agent phases as Claude Code sessions.

Each agent gets a persistent ClaudeSDKClient session tracked in the
registry. Sessions survive restarts and can be resumed, interacted with
from the dashboard, or cancelled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess as sp
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry, SessionRegistry,
)

InputHandler = Callable[[str, dict[str, Any]], str]

log = logging.getLogger(__name__)

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
    run_key: str
    phase: str
    success: bool
    duration_ms: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ""
    final_text: str = ""


def _build_prompt(phase: str, run_key: str, role: str = "", context: str = "") -> str:
    parts = [f"Phase: {phase}", f"Issue: #{run_key}"]

    if context:
        parts.append(context)
    session_name = _session_name(run_key, role=role, phase=phase)
    handoff_path = SessionRegistry.handoff_path(session_name, phase)
    parts.append(
        f"After completing this phase, write your handoff file at "
        f"`{handoff_path}` with your results."
    )
    return "\n\n".join(parts)


def _session_name(run_key: str, role: str = "", phase: str = "") -> str:
    prefix = role.lower() if role else "agent"
    if phase:
        return f"{prefix}-{run_key.lower()}-{phase}"
    return f"{prefix}-{run_key.lower()}"


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------
#
# Agent processes run out-of-band — their own OS process for `modastack
# spawn`, or a worker thread for workflow phases — so they can't reach the
# manager's in-process event queue directly. They post lifecycle events to the
# bus the same way monitor checks do: over HTTP via events/publish.post_event.
# The started emit is fire-and-forget on a daemon thread so a missing or
# unreachable event server never blocks or breaks the agent run. The
# terminal emit (completed/failed) blocks briefly on that thread: it's the
# last action before the agent process exits, and a daemon thread would
# otherwise be killed mid-POST at shutdown.


def _summarize_output(text: str, max_lines: int = 6, max_chars: int = 600) -> str:
    """Last few non-empty lines of an agent's final output, for event summaries."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])[:max_chars]


def _emit_lifecycle_event(
    event_type: str, data: dict[str, Any], *, blocking: bool = False,
    timeout: float = 5,
) -> None:
    """POST an agent lifecycle event to the event bus.

    Runs on a daemon thread and swallows all errors — event delivery is
    best-effort and must never fail the agent run.

    With ``blocking=True`` the caller waits (up to ``timeout`` seconds) for the
    POST to land before returning. This is required for the *terminal* emit
    (session.completed / session.failed): it fires as the last action before the
    spawn process exits, and a daemon thread is killed at interpreter shutdown
    without finishing its in-flight POST. The bounded join can't hang the
    process — ``post_event`` carries its own socket timeout.
    """
    payload = {k: v for k, v in data.items() if v not in (None, "")}

    def _send() -> None:
        try:
            from modastack.events.publish import post_event
            post_event(event_type, payload)
        except Exception as e:  # never let event posting surface
            log.debug(f"Lifecycle event {event_type} not posted: {e}")

    t = threading.Thread(target=_send, daemon=True, name="lifecycle-event")
    t.start()
    if blocking:
        t.join(timeout)  # let the POST land before the process exits


def _emit_session_started(
    run_key: str, project: str, task: str, session_id: str, phase: str = "",
    requested_by: dict | None = None, role: str = "",
) -> None:
    label = role or "Agent"
    _emit_lifecycle_event("agent/session.started", {
        "run_key": run_key,
        "role": role,
        "project": project,
        "task": (task or "")[:500],
        "session_id": session_id,
        "phase": phase,
        "requested_by": requested_by or None,
        "text": f"{label} started working on {run_key}",
    })


def _emit_session_finished(
    result: "AgentResult", project: str, session_id: str, started_at: float,
    requested_by: dict | None = None, role: str = "",
) -> None:
    duration = round(time.time() - started_at, 1)
    label = role or "Agent"
    # Terminal emit: block so the POST lands before the agent process exits.
    if result.success:
        summary = _summarize_output(result.final_text)
        _emit_lifecycle_event("agent/session.completed", {
            "run_key": result.run_key,
            "role": role,
            "project": project,
            "session_id": session_id,
            "phase": result.phase,
            "duration": duration,
            "summary": summary,
            "requested_by": requested_by or None,
            "text": f"{label} finished {result.run_key} in {duration:.0f}s",
        }, blocking=True)
    else:
        error = result.error or "unknown error"
        _emit_lifecycle_event("agent/session.failed", {
            "run_key": result.run_key,
            "role": role,
            "project": project,
            "session_id": session_id,
            "phase": result.phase,
            "duration": duration,
            "error": error,
            "requested_by": requested_by or None,
            "text": f"{label} failed on {result.run_key}: {error}",
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
    run_key: str,
    phase: str,
    timeout: int,
    on_input_needed: InputHandler | None = None,
    role: str = "",
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

    name = _session_name(run_key, role=role, phase=phase)
    saved_id = load_session_id(name)
    registry = get_registry()

    hooks = _make_defer_hook() if on_input_needed else None

    label = role or "agent"
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
                f"You are a {label} agent working on issue #{run_key}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
    )

    client = ClaudeSDKClient(options)
    registry.update(name, status="running", phase=phase, session_id=saved_id or "")

    result = AgentResult(
        session_id="", run_key=run_key, phase=phase, success=False,
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
                log.info(f"Agent {run_key}/{phase} deferred {deferred.name}")
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
        log.error(f"Sub-agent error for {run_key}/{phase}: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


def run_phase_blocking(
    run_key: str,
    phase: str,
    cwd: str,
    context: str = "",
    title: str = "",
    project: str = "",
    timeout: int | None = None,
    role: str = "",
) -> AgentResult:
    """Run a sub-agent phase, blocking until completion.

    Creates a Session, starts with the phase prompt, and blocks until
    the Claude session finishes processing. The session has an inbox
    so other sessions can message it during execution.
    """
    from modastack.session import Session

    prompt = _build_prompt(phase, run_key, role=role, context=context)
    effective_timeout = timeout or PHASE_TIMEOUT.get(phase, 1800)
    name = _session_name(run_key, role=role, phase=phase)

    started_at = time.time()
    _emit_session_started(run_key, project, title or context, name, phase=phase, role=role)

    label = role or "agent"
    append_text = (
        f"You are a {label} agent working on issue #{run_key}, "
        f"phase: {phase}. Follow the skill file instructions exactly."
    )
    memory_prompt = _load_memory_for_session(cwd, name)
    if memory_prompt:
        append_text += "\n\n" + memory_prompt

    session = Session(
        name=name,
        cwd=cwd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": append_text,
        },
        extra_options={"skills": "all", "max_turns": 200},
    )

    ok = session.start(startup_prompt=prompt, timeout=effective_timeout)

    if ok:
        result = AgentResult(
            session_id=session.get_session_id(),
            run_key=run_key,
            phase=phase,
            success=not session._last_is_error,
            duration_ms=session._total_duration_ms,
            total_cost_usd=session._total_cost_usd,
            num_turns=session._total_turns,
            final_text=session._last_response,
            error="" if not session._last_is_error else session._last_response,
        )
    else:
        result = AgentResult(
            session_id="", run_key=run_key, phase=phase,
            success=False, error=f"session failed to start within {effective_timeout}s",
        )

    session.stop()
    _emit_session_finished(result, project, name, started_at, role=role)
    return result



def _resolve_project_name(cwd: str) -> str:
    """Resolve a project name for session naming from the directory name."""
    return Path(cwd).name or cwd


def _load_memory_for_session(cwd: str, session_name: str) -> str:
    """Load the decision log for a session, returning formatted prompt text.

    Returns empty string if no memory exists. Never raises — memory loading
    is best-effort and must not block session startup.
    """
    try:
        from modastack.memory import load_memory, format_memory_prompt
        state_dir = Path(cwd) / ".modastack" / "state"
        content = load_memory(state_dir, session_name)
        return format_memory_prompt(content)
    except Exception:
        log.debug("Failed to load memory for %s", session_name, exc_info=True)
        return ""


def spawn_adhoc(
    cwd: str,
    task: str,
    timeout: int = 3600,
    name: str | None = None,
    requested_by: dict | None = None,
    persistent: bool = False,
    role: str = "",
    mcp_servers: dict | None = None,
) -> AgentResult:
    """Spawn an agent with a freeform task prompt.

    Creates a Session with the task as the startup prompt. The session
    has an inbox so other sessions can message it during execution.

    With ``persistent=True`` the session stays alive after the initial
    task completes, accepting messages via its inbox until explicitly
    stopped. The caller blocks for the lifetime of the session.
    """
    import hashlib
    from modastack.session import Session

    short_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
    run_key = name or f"adhoc-{short_hash}"
    project = _resolve_project_name(cwd)
    requested_by = requested_by or {}

    started_at = time.time()
    _emit_session_started(run_key, project, task, run_key, phase="adhoc",
                          requested_by=requested_by, role=role)

    from modastack.prompts.resolver import _resolve_role_prompt
    role_prompt = _resolve_role_prompt(role, Path(cwd))
    label = role or "agent"
    append_parts = [
        f"You are a {label} agent working on an adhoc task. "
        f"Complete the task described in your initial prompt."
    ]
    if persistent:
        append_parts.append(
            "After completing the initial task, stay available — "
            "you will receive follow-up messages via your inbox."
        )
    if role_prompt:
        append_parts.append(role_prompt)

    # Inject decision log (memory) so the session has continuity.
    # Skip if the task prompt already contains it (e.g. entry-point agent
    # where build_startup_prompt() already injected memory).
    if "## Decision Log" not in task:
        memory_prompt = _load_memory_for_session(cwd, run_key)
        if memory_prompt:
            append_parts.append(memory_prompt)

    session = Session(
        name=run_key,
        cwd=cwd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(append_parts),
        },
        extra_options={
            "skills": "all",
            "max_turns": 200,
            **({"mcp_servers": mcp_servers} if mcp_servers else {}),
        },
        role=role,
    )

    ok = session.start(startup_prompt=task, timeout=timeout)

    if persistent and ok:
        try:
            session._thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            session.stop()

        result = AgentResult(
            session_id=session.get_session_id(),
            run_key=run_key,
            phase="adhoc",
            success=True,
            duration_ms=session._total_duration_ms,
            total_cost_usd=session._total_cost_usd,
            num_turns=session._total_turns,
            final_text=session._last_response,
        )
        _emit_session_finished(result, project, run_key, started_at,
                               requested_by=requested_by, role=role)
        return result

    if ok:
        result = AgentResult(
            session_id=session.get_session_id(),
            run_key=run_key,
            phase="adhoc",
            success=not session._last_is_error,
            duration_ms=session._total_duration_ms,
            total_cost_usd=session._total_cost_usd,
            num_turns=session._total_turns,
            final_text=session._last_response,
        )
    else:
        result = AgentResult(
            session_id="", run_key=run_key, phase="adhoc",
            success=False, error=f"session failed to start within {timeout}s",
        )

    session.stop()
    _emit_session_finished(result, project, run_key, started_at,
                           requested_by=requested_by, role=role)
    return result


def _launch_detached(script: str, args: list[str], log_file: Path) -> int:
    """Launch a detached subprocess that survives parent exit. Returns pid."""
    cmd = [sys.executable, "-c", script, *args]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as lf:
        proc = sp.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
    return proc.pid


def launch_agent(
    task: str,
    cwd: str,
    workflow_name: str,
    timeout: int = 3600,
    requested_by: dict | None = None,
    interactive: bool = True,
    role: str = "",
    persistent: bool = False,
    subscribe: list[str] | None = None,
    run_key: str | None = None,
) -> str:
    """Launch an agent as a detached subprocess and return immediately.

    Session name is deterministic: wf-{workflow}-{project}-{run_key}.
    - If an active run exists for the same session → reject
    - If a failed/stale run exists → resume (same session ID)
    - If completed or new → fresh start

    With ``persistent=True``, the agent stays alive after its initial
    task, accepting messages via its inbox. Uses spawn_adhoc() directly
    instead of the workflow orchestrator.
    """
    import uuid
    run_key = run_key or f"adhoc-{uuid.uuid4().hex[:8]}"
    project = _resolve_project_name(cwd)

    if persistent:
        session_name = run_key
    else:
        from modastack.workflow.orchestrator import make_session_name
        session_name = make_session_name(workflow_name, project, run_key)

    registry = get_registry()
    existing = registry.get(session_name)
    if existing and existing.status in ("starting", "running", "idle"):
        raise RuntimeError(
            f"A run is already active: {session_name} (status={existing.status}). "
            f"Cancel it first or wait for it to complete."
        )

    args_json = json.dumps({
        "task": task,
        "cwd": cwd,
        "workflow_name": workflow_name,
        "timeout": timeout,
        "requested_by": requested_by or {},
        "run_key": run_key,
        "interactive": interactive,
        "role": role,
        "persistent": persistent,
        "subscribe": subscribe or [],
    })
    script = (
        "import json, sys; "
        "from modastack.subagent import _run_agent_entry; "
        "_run_agent_entry(json.loads(sys.argv[1]))"
    )

    # Auto-rotate when the installed image has changed since the last run.
    from modastack.sdk import check_image_rotation, compute_manifest_hash
    check_image_rotation(session_name, Path(cwd))

    # Register first so the session dir exists for the log file
    registry.register(SessionEntry(
        name=session_name, session_id="", role=role,
        run_key=run_key, title=task[:80], phase=workflow_name,
        project=project, cwd=cwd, status="starting",
        requested_by=requested_by or {},
        image_hash=compute_manifest_hash(Path(cwd)),
    ))

    log_file = SessionRegistry.log_path(session_name)
    pid = _launch_detached(script, [args_json], log_file)
    registry.update(session_name, pid=pid)
    return session_name


def _start_event_subscription(session_name: str, subscribe: list[str],
                               project_path: Path) -> None:
    """Start event client + drain loop for a subscribing agent."""
    from modastack.config import Config, load_deployment_state, save_deployment_state
    from modastack.events.client import EventServerClient
    from modastack.events.drain import drain_loop
    from modastack.events.server import ensure_running, register

    cfg = Config.load(project_path)
    es_url = cfg.event_server_url
    state = load_deployment_state(project_path)
    es_key = state.get("api_key", "")
    es_deployment = state.get("deployment_id", "")

    def _register_with_retry(url: str, attempts: int = 3) -> tuple[str, str]:
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                dep, key = register(url, session_name, subscribe)
                save_deployment_state(project_path, dep, key)
                return dep, key
            except Exception as e:
                last_err = e
                if attempt < attempts - 1:
                    delay = 2 ** (attempt + 1)
                    log.warning(
                        "Event server registration failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, attempts, e, delay,
                    )
                    time.sleep(delay)
        raise RuntimeError(
            f"Could not register with event server at {url} "
            f"after {attempts} attempts: {last_err}"
        ) from last_err

    if not es_url:
        es_port = 8080
        es_url = f"http://localhost:{es_port}"
        result = ensure_running(es_port, project_path=project_path)
        if result == "started":
            log.info("No event server configured — started local server on port %d", es_port)
        elif result == "connected":
            log.info("Connected to existing local event server on port %d", es_port)
        es_deployment, es_key = _register_with_retry(es_url)
    elif not (es_deployment and es_key):
        # No saved deployment — register fresh rather than PUT to a
        # guaranteed-400 empty deployment URL.
        es_deployment, es_key = _register_with_retry(es_url)
    else:
        import json as _json, urllib.request
        try:
            req = urllib.request.Request(
                f"{es_url}/deployments/{es_deployment}/subscriptions",
                data=_json.dumps({"add": subscribe}).encode(),
                headers={
                    "Authorization": f"Bearer {es_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "modastack",
                },
                method="PUT",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.info("Subscription update failed (%s) — re-registering", e)
            es_deployment, es_key = _register_with_retry(es_url)

    client = EventServerClient(
        server_url=es_url,
        deployment_id=es_deployment,
        api_key=es_key,
    )
    client.start()

    drain_thread = threading.Thread(
        target=drain_loop, args=(session_name,),
        daemon=True, name="agent-drain",
    )
    drain_thread.start()
    log.info(f"Event subscription started for {session_name}: {subscribe}")


def _run_agent_entry(args: dict) -> None:
    """Entry point for the detached subprocess. Runs the orchestrator."""
    task = args["task"]
    cwd = args["cwd"]
    workflow_name = args["workflow_name"]
    timeout = args.get("timeout", 3600)
    requested_by = args.get("requested_by", {})
    run_key = args.get("run_key", "adhoc")
    interactive = args.get("interactive", True)
    role = args.get("role", "")
    persistent = args.get("persistent", False)
    subscribe = args.get("subscribe", [])

    from modastack.sdk import set_project_root
    project_root = Path(cwd).resolve()
    set_project_root(project_root)

    if subscribe and project_root:
        _start_event_subscription(run_key, subscribe, project_root)

    if persistent:
        spawn_adhoc(
            cwd=cwd,
            task=task,
            timeout=timeout,
            name=run_key,
            requested_by=requested_by,
            persistent=True,
            role=role,
        )
        return

    from modastack.workflow.orchestrator import run_workflow
    from modastack.workflow.triggers import WorkflowDispatcher

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    workflow = dispatcher.find_workflow(workflow_name)
    if not workflow:
        print(f"Workflow '{workflow_name}' not found")
        return

    project = _resolve_project_name(cwd)
    run_workflow(
        workflow=workflow,
        task=task,
        repo=project,
        cwd=cwd,
        run_key=run_key,
        requested_by=requested_by,
        timeout=timeout,
        interactive=interactive,
        role=role,
    )


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

    Reuses the same supervised agent loop as agent phases, but with a
    constrained read-only prompt and no input handler. Blocks until the
    agent finishes or times out.
    """
    import hashlib

    short_hash = hashlib.sha256(description.encode()).hexdigest()[:8]
    slug = name or f"check-{short_hash}"
    run_key = slug
    phase = "check"
    session = _session_name(run_key, role="monitor", phase=phase)

    prompt = _build_check_prompt(description, extra)

    registry = get_registry()
    registry.register(SessionEntry(
        name=session, session_id="", role="monitor",
        run_key=run_key, title=description[:80], phase=phase,
        cwd=cwd, status="starting",
    ))

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_agent_supervised(prompt, cwd, run_key, phase, timeout, role="monitor"),
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
# Agent inspection — registry-backed
# ---------------------------------------------------------------------------


def list_agents() -> list[dict[str, Any]]:
    """List active agents from the on-disk SessionRegistry.

    Detached agents (launched via launch_agent into child repos) register
    in the runtime root's SessionRegistry, so they are visible from any
    process resolving the same runtime root.
    """
    result = []
    try:
        registry = get_registry()
    except Exception:
        return result  # registry may not be initialized yet
    for entry in registry.list_active():
        if entry.role == "manager":
            continue  # managers are shown separately in `modastack status`
        result.append({
            "run_key": entry.run_key or entry.name,
            "phase": entry.phase,
            "cwd": entry.cwd,
            "running": True,
            "elapsed_s": int(time.time() - entry.started_at),
            "name": entry.name,
            "source": "registry",
        })
    return result


def find_agent(ref: str) -> SessionEntry | None:
    """Look up a registry entry by session name or run key (active first)."""
    registry = get_registry()
    entry = registry.get(ref)
    if entry:
        return entry
    ref_lower = ref.lower()
    candidates = [e for e in registry.list_all()
                  if e.run_key.lower() == ref_lower or e.name.lower() == ref_lower]
    if not candidates:
        return None
    active = [e for e in candidates if e.status in ("starting", "running", "idle")]
    pool = active or candidates
    return max(pool, key=lambda e: e.last_activity)


def cancel_agent(ref: str) -> bool:
    """Cancel a running agent by session name or run key.

    Terminates the detached process (if its pid is alive) and marks the
    registry entry cancelled.
    """
    import os
    import signal

    entry = find_agent(ref)
    if not entry or entry.status not in ("starting", "running", "idle"):
        return False
    if entry.pid:
        try:
            os.kill(entry.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    get_registry().update(entry.name, status="cancelled", pid=0)
    log.info(f"Sub-agent cancelled: {entry.name}")
    return True
