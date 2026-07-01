"""Sub-agent executor — runs agent phases as Claude Code sessions.

Each agent gets a persistent ClaudeSDKClient session tracked in the
registry. Sessions survive restarts and can be resumed, interacted with
from the dashboard, or cancelled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess as sp
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bobi.sdk import (
    save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry, SessionRegistry,
    TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED,
)
from bobi.transient import is_transient_api_error
from bobi.env import (
    _configured_brain_kind,
    agent_spawn_env,
    child_agent_env,
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
    # Whether a failure was a transient API error (529/rate-limit/5xx). Set from
    # the shared classifier (bobi.transient) so the spawn path and the
    # persistent session agree on "transient" — the launcher's re-dispatch
    # decision can consult it. Survival/retry stays at the #444 layer (§4.3).
    transient: bool = False


def _network_drop_error(detail: str = "") -> str:
    base = "network drop: response stream ended before turn result"
    return f"{base} ({detail})" if detail else base


def _timeout_error(timeout: int | None = None) -> str:
    if timeout is None:
        return "subprocess timeout while draining response"
    return f"subprocess timeout after {timeout}s"


def _tool_crash_error(error: BaseException | str) -> str:
    message = str(error).strip() or error.__class__.__name__
    if message.startswith("tool crash:"):
        return message
    return f"tool crash: {message}"


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
# Agent processes run out-of-band — their own OS process for `bobi
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
) -> bool:
    """POST an agent lifecycle event to the event bus.

    Runs on a daemon thread and swallows all errors — event delivery is
    best-effort and must never fail the agent run.

    With ``blocking=True`` the caller waits (up to ``timeout`` seconds) for the
    POST to land before returning. This is required for the *terminal* emit
    (session.completed / session.failed): it fires as the last action before the
    spawn process exits, and a daemon thread is killed at interpreter shutdown
    without finishing its in-flight POST. The bounded join can't hang the
    process — ``post_event`` carries its own socket timeout.

    Returns whether the POST is known to have landed (only meaningful with
    ``blocking=True``; a non-blocking emit always returns False since the result
    is unknown). The terminal-emit path uses this to mark ``emit_confirmed`` so
    the reconciler re-emits only the completions whose POST never landed —
    never double-delivering a healthy one (MDS-65 RC#3, §4.6).
    """
    payload = {k: v for k, v in data.items() if v not in (None, "")}
    result = {"ok": False}

    def _send() -> None:
        try:
            from bobi.events.publish import post_event
            post_event(event_type, payload)
            result["ok"] = True
        except Exception as e:  # never let event posting surface
            log.debug(f"Lifecycle event {event_type} not posted: {e}")

    t = threading.Thread(target=_send, daemon=True, name="lifecycle-event")
    t.start()
    if blocking:
        t.join(timeout)  # let the POST land before the process exits
        return result["ok"]
    return False


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
    # The 3rd positional ``session_id`` is the registry ENTRY NAME (callers pass
    # the session name). Durably record the honest terminal status to state.json
    # BEFORE the best-effort bus POST (RC#3), so a swallowed emit never loses the
    # outcome; then mark emit_confirmed only if the POST actually landed, so the
    # reconciler re-emits exactly the completions that didn't reach the bus.
    name = session_id
    registry = get_registry()
    terminal = TERMINAL_COMPLETED if result.success else TERMINAL_FAILED
    _persist_terminal(registry, name, terminal, error=result.error,
                      session_id=result.session_id or "", phase=result.phase)

    if result.success:
        summary = _summarize_output(result.final_text)
        landed = _emit_lifecycle_event("agent/session.completed", {
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
        landed = _emit_lifecycle_event("agent/session.failed", {
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

    if landed:
        try:
            registry.update(name, emit_confirmed=True)
        except Exception:
            log.debug("emit_confirmed update failed for %s", name, exc_info=True)


def _persist_terminal(registry, name: str, status: str, *, error: str = "",
                      session_id: str = "", phase: str = "") -> None:
    """Durably record an honest terminal status to ``state.json`` (MDS-65 RC#3).

    Written synchronously to local disk *before* and independent of the
    best-effort lifecycle bus POST, so a swallowed emit (flaky event server, a
    daemon thread killed mid-POST at shutdown) never loses the outcome. The
    reconciler reads ``state.json`` as the source of truth and re-emits any
    terminal run whose emit was never confirmed. Best-effort itself: a registry
    write failure must not mask the agent's real result.
    """
    try:
        registry.mark_terminal(
            name, status, error=error,
            session_id=session_id or None, phase=phase or None,
        )
    except Exception:  # never let bookkeeping surface over the agent result
        # A failed persist defeats the reconciler backstop (state.json is the
        # durable source of truth), so this is worth a warning, not just debug.
        log.warning("Terminal status persist failed for %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Blocking execution (new executor path)
# ---------------------------------------------------------------------------


def _make_defer_hook() -> dict:
    """PreToolUse hook that defers AskUserQuestion so we can route it.

    Claude-specific: the hook/HookMatcher API is the only SDK surface left
    outside ``bobi.brain``. It rides through to the brain as an ``hooks``
    option (a no-op for brains without a hook system). Whether non-Claude brains
    need interactive deferral at all is #485 open Q5.
    """
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
    max_turns: int = 200,
) -> AgentResult:
    """Core agent loop. Blocks until the agent finishes or times out.

    When on_input_needed is provided, AskUserQuestion calls are deferred
    via a PreToolUse hook. The deferred question is routed through the
    callback, and the agent is resumed with the answer.

    Unlike Session-backed agents, this path runs a raw ``ClaudeSDKClient``
    with no inbox and no ``inbox/<self>`` subscription, so it is **not
    addressable** over the event server. That is intentional: its only caller
    is the out-of-band monitor check (``run_check_blocking``), a short-lived,
    read-only, observe-and-report agent that no one needs to message mid-run.
    Any agent that must be reachable goes through ``Session`` instead.
    """
    from bobi.brain import AssistantText, TurnResult, get_brain

    name = _session_name(run_key, role=role, phase=phase)
    saved_id = load_session_id(name)
    registry = get_registry()

    hooks = _make_defer_hook() if on_input_needed else None

    label = role or "agent"
    client = get_brain().make_session(
        cwd=cwd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                f"You are a {label} agent working on issue #{run_key}, "
                f"phase: {phase}. Follow the skill file instructions exactly."
            ),
        },
        resume=saved_id or None,
        options={"max_turns": max_turns, "hooks": hooks, "skills": "all"},
    )
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
                if isinstance(msg, AssistantText):
                    if msg.text:
                        result.final_text = msg.text
                        log_activity("response", {
                            "text": msg.text[:500],
                        }, session=name)
                elif isinstance(msg, TurnResult):
                    result_msg = msg

            if result_msg is None:
                result.error = _network_drop_error("no ResultMessage")
                _persist_terminal(registry, name, TERMINAL_FAILED,
                                  error=result.error, phase=phase)
                return result

            save_session_id(name, result_msg.session_id)
            result.session_id = result_msg.session_id
            result.duration_ms += result_msg.duration_ms
            result.total_cost_usd += result_msg.total_cost_usd or 0.0
            result.num_turns += result_msg.num_turns

            if result_msg.deferred_tool and on_input_needed:
                deferred = result_msg.deferred_tool
                log.info(f"Agent {run_key}/{phase} deferred {deferred.name}")
                loop = asyncio.get_running_loop()
                answer = await loop.run_in_executor(
                    None, on_input_needed, deferred.name, deferred.input,
                )
                await client.query(answer)
                continue

            result.success = not result_msg.is_error
            if result_msg.is_error:
                result.error = result_msg.result_text or "unknown error"
                # Single-sourced transient classification (§4.3): a 529/rate-limit
                # /5xx is tagged transient so the launcher can re-dispatch. We do
                # NOT retry here — survival/retry is owned by #444.
                result.transient = is_transient_api_error(
                    result_msg.api_error_status,
                    result_msg.result_text or "",
                )
            # RC#2: honest terminal status — never record `done` on an error
            # result. A transient 529 surfaces as an error ResultMessage (not an
            # exception), so the old unconditional `done` wrote a success over a
            # real failure. We record it honestly as `failed` and let it be
            # delivered (RC#1); transient survival/retry is owned by the
            # persistent session (#444), so the spawn path adds no retry (§4.3).
            terminal = TERMINAL_COMPLETED if result.success else TERMINAL_FAILED
            _persist_terminal(registry, name, terminal, error=result.error,
                              session_id=result_msg.session_id, phase=phase)
            log_activity("stop", {"session_id": result_msg.session_id,
                                  "status": terminal}, session=name)
            return result

    except asyncio.TimeoutError:
        result.error = _timeout_error(timeout)
        _persist_terminal(registry, name, TERMINAL_FAILED, error=result.error,
                          phase=phase)
    except Exception as e:
        result.error = _tool_crash_error(e)
        # An unhandled executor exception is a crash, not a clean failure.
        _persist_terminal(registry, name, TERMINAL_CRASHED, error=result.error,
                          phase=phase)
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
    requested_by: dict | None = None,
) -> AgentResult:
    """Run a sub-agent phase, blocking until completion.

    Creates a Session, starts with the phase prompt, and blocks until
    the Claude session finishes processing. The session has an inbox
    so other sessions can message it during execution.

    ``requested_by`` is threaded onto the started/finished lifecycle events so
    a completion can be routed back to the requester's thread (MDS-65 RC#4) —
    the non-persistent phase path previously dropped it entirely.
    """
    from bobi.session import Session

    prompt = _build_prompt(phase, run_key, role=role, context=context)
    effective_timeout = timeout or PHASE_TIMEOUT.get(phase, 1800)
    name = _session_name(run_key, role=role, phase=phase)

    started_at = time.time()
    _emit_session_started(run_key, project, title or context, name, phase=phase,
                          requested_by=requested_by, role=role)

    label = role or "agent"
    append_text = (
        f"You are a {label} agent working on issue #{run_key}, "
        f"phase: {phase}. Follow the skill file instructions exactly."
    )
    policy_prompt = _load_policy_prompt()
    if policy_prompt:
        append_text += "\n\n" + policy_prompt

    # Pass through any user-declared MCP servers from config so workflow
    # step agents also have access to them.
    from bobi.paths import bobi_root as _mr
    from bobi.config import Config as _Config
    try:
        _cfg = _Config.load(_mr())
        _mcp = _cfg.mcp_servers
    except Exception:
        _mcp = None

    session = Session(
        name=name,
        cwd=cwd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": append_text,
        },
        extra_options={
            "skills": "all",
            "max_turns": 200,
            **({"mcp_servers": _mcp} if _mcp else {}),
        },
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
    _emit_session_finished(result, project, name, started_at,
                           requested_by=requested_by, role=role)
    return result



def _resolve_project_name(cwd: str) -> str:
    """Resolve a project name for session naming.

    Runtime-scoped CLI launches run from ``<agent>/run``; naming those sessions
    after the literal directory would collapse every machine-scoped launch to
    ``run``. Use the selected Bobi Agent name for the bound runtime root, while
    preserving ordinary repo-directory names for agents launched against a
    specific checkout.
    """
    path = Path(cwd).resolve()
    try:
        from bobi import paths
        root = paths.bobi_root().resolve()
        if path == root:
            return paths.agent_name_for_root(root)
    except Exception:
        pass
    return path.name or cwd


def _load_policy_prompt() -> str:
    """Load the team policy.md, returning read-only formatted prompt text (#456).

    Team-scoped — the same curated policy for every session. Returns empty
    string when policy.md is absent. Never raises — policy loading is
    best-effort and must not block session startup.
    """
    try:
        from bobi import paths
        from bobi.memory import load_policy, format_policy_prompt
        content = load_policy(paths.state_path())
        return format_policy_prompt(content)
    except Exception:
        log.debug("Failed to load policy", exc_info=True)
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
    subscribe: list[str] | None = None,
) -> AgentResult:
    """Spawn an agent with a freeform task prompt.

    Creates a Session with the task as the startup prompt. The session
    has an inbox so other sessions can message it during execution.

    ``subscribe`` adds event topics beyond the session's own ``inbox/<self>``
    (the manager passes its external resource topics here).

    With ``persistent=True`` the session stays alive after the initial
    task completes, accepting messages via its inbox until explicitly
    stopped. The caller blocks for the lifetime of the session.
    """
    import hashlib
    from bobi.session import Session

    short_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
    run_key = name or f"adhoc-{short_hash}"
    project = _resolve_project_name(cwd)
    requested_by = requested_by or {}

    started_at = time.time()
    _emit_session_started(run_key, project, task, run_key, phase="adhoc",
                          requested_by=requested_by, role=role)

    from bobi.paths import bobi_root
    from bobi.prompts.resolver import _resolve_role_prompt
    # Roles live at the installation root; cwd is the agent's working dir.
    role_prompt = _resolve_role_prompt(role, bobi_root())
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

    # Inject the team policy (#456) so the session has continuity.
    # Skip if the task prompt already contains it (e.g. entry-point agent
    # where build_startup_prompt() already injected the policy).
    if "## Team Policy" not in task:
        policy_prompt = _load_policy_prompt()
        if policy_prompt:
            append_parts.append(policy_prompt)

    # Resolve MCP servers: caller-supplied override, else config-declared.
    # Done here so all spawn paths (CLI, workflow, subagent) go through one
    # call site.
    from bobi.paths import bobi_root as _mr
    from bobi.config import Config as _Config
    try:
        _cfg = _Config.load(_mr())
        merged_mcp = mcp_servers or _cfg.mcp_servers
    except Exception:
        merged_mcp = mcp_servers

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
            **({"mcp_servers": merged_mcp} if merged_mcp else {}),
        },
        role=role,
        subscribe=subscribe,
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


def _launch_detached(script: str, args: list[str], log_file: Path,
                     env: dict[str, str] | None = None) -> int:
    """Launch a detached subprocess that survives parent exit. Returns pid."""
    cmd = [sys.executable, "-c", script, *args]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as lf:
        proc = sp.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True,
                        env=env)
    return proc.pid


# ---------------------------------------------------------------------------
# Requires: dispatch-time preflight gate
# ---------------------------------------------------------------------------

_requires_cache: dict[str, tuple[float, list]] = {}
_REQUIRES_TTL = 120  # seconds


def check_requires(project_path: Path) -> list[tuple]:
    """Run package requires checks with a short-TTL cache.

    Returns list of (RequiresEntry, passed, detail) tuples.
    Cached results are reused within the TTL to avoid latency
    growth when multiple agents dispatch in quick succession.
    """
    key = str(project_path)
    now = time.time()
    cached = _requires_cache.get(key)
    if cached and (now - cached[0]) < _REQUIRES_TTL:
        return cached[1]

    from bobi.config import Config, run_requires_checks
    try:
        cfg = Config.load(project_path)
    except Exception:
        return []
    if not cfg.requires:
        return []

    results = run_requires_checks(cfg.requires)
    _requires_cache[key] = (now, results)
    return results


def _alert_requires_failure(
    project_path: Path,
    failures: list[tuple],
) -> None:
    """Post a Slack alert about failed requires checks. Best-effort."""
    try:
        from bobi.config import Config
        cfg = Config.load(project_path)
        slack_svc = next(
            (s for s in cfg.services if s.name == "slack" and s.channels),
            None,
        )
        if not slack_svc:
            log.warning("No Slack service with channels configured — "
                        "cannot alert on requires failure")
            return
        token = slack_svc.credentials.get("bot_token", "")
        if not token:
            log.warning("Slack bot_token not configured — "
                        "cannot alert on requires failure")
            return
        channel = slack_svc.channels[0]
        lines = []
        for entry, detail in failures:
            line = f"*{entry.name}*: {entry.why or detail}"
            if entry.fix:
                line += f"\nFix: `{entry.fix}`"
            lines.append(line)
        msg = (
            "\u26a0\ufe0f Agent dispatch blocked — required dependency "
            "check failed on this host.\n\n" + "\n\n".join(lines)
        )
        from bobi.slack import post_slack_message
        post_slack_message(token, channel, msg)
    except Exception:
        log.warning("Failed to send Slack alert for requires failure",
                     exc_info=True)


def _check_concurrency_semaphore(root: Path, timeout: float = 120) -> None:
    """Block launch until a concurrency slot opens, or raise on timeout.

    Loads the cap from agent.yaml (``max_concurrent_agents`` field,
    default 2). When the cap is reached, the caller queues — polling
    every few seconds until a slot opens or the timeout expires.
    """
    from bobi.config import Config
    from bobi.concurrency_semaphore import (
        DEFAULT_CAP, check_concurrency, wait_for_slot,
        emit_concurrency_cap_alert,
    )
    try:
        cfg = Config.load(root)
    except Exception:
        return  # can't load config — don't block
    cap = cfg.max_concurrent_agents or DEFAULT_CAP
    if cap < 1:
        # A misconfigured 0/negative cap would queue every launch until it
        # times out; fall back to the default rather than wedging all dispatch.
        cap = DEFAULT_CAP
    allowed, count = check_concurrency(cap)
    if allowed:
        return
    # At capacity — emit an alert and queue (block) until a slot opens.
    emit_concurrency_cap_alert(count, cap)
    if not wait_for_slot(cap, timeout):
        raise RuntimeError(
            f"Concurrency semaphore: {count} agents running "
            f"(cap: {cap}). Timed out waiting for a slot after "
            f"{timeout:.0f}s. Cancel an active agent or raise the cap."
        )


def _check_spend_governor(root: Path) -> None:
    """Block launch if the rolling-hour invocation cap is exceeded.

    Loads the cap from agent.yaml (``spend_cap`` field, default 50).
    On breach, emits a ``system/spend.cap.breached`` alert event and
    raises RuntimeError to prevent the launch.
    """
    from bobi.config import Config
    from bobi.spend_governor import (
        DEFAULT_CAP, check_spend_cap, emit_spend_cap_alert,
    )
    try:
        cfg = Config.load(root)
    except Exception:
        return  # can't load config — don't block
    cap = cfg.spend_cap or DEFAULT_CAP
    allowed, count = check_spend_cap(root, cap)
    if not allowed:
        emit_spend_cap_alert(root, count, cap)
        raise RuntimeError(
            f"Spend governor: {count} agent invocations in the last hour "
            f"(cap: {cap}). New launches are blocked until invocations "
            f"age out of the rolling window."
        )


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
    input_fields: dict | None = None,
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
        from bobi.workflow.orchestrator import make_session_name
        session_name = make_session_name(workflow_name, project, run_key)

    registry = get_registry()
    existing = registry.get(session_name)
    if existing and existing.status in ("starting", "running", "idle"):
        raise RuntimeError(
            f"A run is already active: {session_name} (status={existing.status}). "
            f"Cancel it first or wait for it to complete."
        )

    # The installation root travels with the spawn explicitly. cwd is the
    # agent's WORKING dir (often a repo checkout) and must not double as
    # its identity — agent.yaml, install-manifest.json, and workflows all
    # live at the root, not wherever the agent happens to work. The
    # spawning process bound its root at its entry point; an unbound
    # process here is a bug and raises rather than guessing.
    from bobi.paths import bobi_root
    root = bobi_root()

    # Preflight: check host-level dependencies declared in agent.yaml
    req_results = check_requires(root)
    req_failures = [(entry, detail) for entry, ok, detail in req_results if not ok]
    if req_failures:
        _alert_requires_failure(root, req_failures)
        names = ", ".join(e.name for e, _ in req_failures)
        raise RuntimeError(
            f"Required dependency check failed: {names}. "
            f"Run `bobi agent <name> doctor` for details and fix commands."
        )

    # Preflight: spend governor — cap agent invocations per rolling hour
    _check_spend_governor(root)

    # Preflight: concurrency semaphore — queue if too many agents running
    _check_concurrency_semaphore(root)

    args_json = json.dumps({
        "task": task,
        "cwd": cwd,
        "root": str(root),
        "workflow_name": workflow_name,
        "timeout": timeout,
        "requested_by": requested_by or {},
        "run_key": run_key,
        "interactive": interactive,
        "role": role,
        "persistent": persistent,
        "subscribe": subscribe or [],
        "input_fields": input_fields or {},
    })
    script = (
        "import json, sys; "
        "from bobi.subagent import _run_agent_entry; "
        "_run_agent_entry(json.loads(sys.argv[1]))"
    )

    # Auto-rotate when the installed image has changed since the last run.
    from bobi.sdk import check_image_rotation, compute_manifest_hash
    check_image_rotation(session_name, root)

    # Register first so the session dir exists for the log file
    registry.register(SessionEntry(
        name=session_name, session_id="", role=role,
        run_key=run_key, title=task[:80], phase=workflow_name,
        project=project, cwd=cwd, status="starting",
        requested_by=requested_by or {},
        image_hash=compute_manifest_hash(root),
        # Persist the declared timeout so the dead-man reconciler knows this
        # run's deadline (MDS-65 §4.6).
        timeout=timeout,
    ))

    log_file = SessionRegistry.log_path(session_name)
    # child_agent_env() is the single parent-to-child propagation contract:
    # identity, brain selection, tool PATH, and credential material all flow
    # through one helper instead of one-off launch-site patches.
    child_env = child_agent_env(root)
    pid = _launch_detached(script, [args_json], log_file, env=child_env)
    registry.update(session_name, pid=pid)

    # Record the invocation for the spend governor's rolling window.
    from bobi.spend_governor import record_invocation
    record_invocation(root)
    return session_name


@dataclass
class Subscription:
    """Teardownable handle for a session's event subscription.

    Owns the WebSocket client + drain thread + queue so ``Session.stop()`` can
    shut them down. Without this, each session leaked a live WS connection and a
    blocked drain thread, and a same-name restart in one process left the old
    drain pushing duplicates into the new inbox.
    """

    client: "Any"
    drain_thread: "threading.Thread"
    queue: "Any"

    def stop(self, timeout: float = 5.0) -> None:
        try:
            self.client.stop()
        except Exception:
            log.debug("Event client stop failed", exc_info=True)
        # Poison-pill the drain so its blocking queue.get() returns.
        from bobi.events.drain import _DRAIN_STOP
        try:
            self.queue.put(_DRAIN_STOP)
            self.drain_thread.join(timeout=timeout)
        except Exception:
            log.debug("Drain thread stop failed", exc_info=True)


_self_github_login: str | None = None
_self_github_login_resolved = False


def _resolve_self_github_login() -> str | None:
    """Best-effort lookup of the bot's own GitHub login via ``gh api user``.

    Cached for the process lifetime. Returns None when ``gh`` is unavailable or
    unauthenticated — the reactor's self-author guard then stays inactive
    (fail open) rather than dropping events. Used to skip auto-dispatching
    pr-feedback on the bot's own comments (issue #411).
    """
    global _self_github_login, _self_github_login_resolved
    if _self_github_login_resolved:
        return _self_github_login
    _self_github_login_resolved = True
    try:
        result = sp.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            login = result.stdout.strip()
            _self_github_login = login or None
            if _self_github_login:
                log.info("Resolved bot GitHub login: %s", _self_github_login)
    except (OSError, sp.SubprocessError) as e:
        log.info("Could not resolve bot GitHub login (self-author guard off): %s", e)
    return _self_github_login


def _start_event_subscription(session_name: str, subscribe: list[str],
                               project_path: Path,
                               register_attempts: int = 3) -> "Subscription":
    """Start event client + drain loop for a subscribing agent.

    Every session subscribes — at minimum to its own ``inbox/<self>`` topic, so
    it is addressable for inter-agent messages. Sessions that also subscribe to
    external resource topics (the manager: ``github:…``, ``slack:…``, monitor
    topics) additionally register their Slack workspace and load the
    auto-dispatch reactor; an inbox-only session skips both — it neither ingests
    Slack nor needs to route external triggers.

    Each session registers its OWN event-server deployment, scoped to
    exactly its subscribe list. Deployments are never shared between
    sessions: the server fans every matching event out to every WebSocket
    on a deployment, so a shared deployment unions all sessions'
    subscriptions and every agent receives everyone's events (the incident
    where every project lead received and answered the user's Slack DMs
    to the director).
    """
    from bobi.config import (
        Config, load_deployment_state, save_deployment_state,
        session_cursor_path, bubble_state_path,
    )
    from bobi.events.client import EventServerClient
    from bobi.events.drain import drain_loop
    from bobi.events.server import (
        ensure_running, ensure_bubble, register, register_slack_workspaces,
        authorize_resources, BubbleRejected,
    )

    cfg = Config.load(project_path)
    es_url = cfg.event_server_url
    # A session that subscribes to anything beyond its own inbox ingests external
    # resources (the manager). Only such a session registers the Slack bot and
    # runs the auto-dispatch reactor; an inbox-only worker skips both. Computed
    # up front because #488 resource authorization (below) runs BEFORE register.
    has_external = any(not k.startswith("inbox/") for k in subscribe)
    state = load_deployment_state(project_path, session_name)
    es_key = state.get("api_key", "")
    es_deployment = state.get("deployment_id", "")
    cursor_path = session_cursor_path(project_path, session_name)

    def _authorize_subscriptions(url: str, bubble: dict) -> list[str]:
        """#488: obtain resource grants BEFORE register/PUT so the server's grant
        check passes. The signed Slack registration writes BOTH the bubble-scoped
        outbound record (#487) and the slack resource grant; github/linear are
        authorized via /resources/authorize. Returns ``subscribe`` filtered to
        drop any global topic we could not authorize (so register/PUT is never
        hard-rejected for a topic we already know is unbacked)."""
        if has_external:
            # Best-effort: a Slack registration hiccup must not block the rest.
            try:
                register_slack_workspaces(
                    url, cfg,
                    bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
                )
            except Exception as e:
                log.info("Signed Slack registration unavailable (%s) — unsigned", e)
                register_slack_workspaces(url, cfg)
        return authorize_resources(
            url, cfg, subscribe, bubble["bubble_id"], bubble["bubble_key"],
        )

    def _register_with_retry(url: str, attempts: int = register_attempts) -> tuple[str, str]:
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                # Every session JOINs the instance's one bubble (minted once,
                # lock-protected, by whichever register fires first). If the
                # server forgot the bubble (restart), re-mint and re-join.
                bubble = ensure_bubble(url, project_path)
                authorized = _authorize_subscriptions(url, bubble)
                try:
                    dep, key = register(
                        url, session_name, authorized,
                        bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
                    )
                except BubbleRejected:
                    bubble = ensure_bubble(url, project_path,
                                           force_remint_of=bubble["bubble_id"])
                    authorized = _authorize_subscriptions(url, bubble)
                    dep, key = register(
                        url, session_name, authorized,
                        bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
                    )
                save_deployment_state(project_path, session_name, dep, key)
                # A fresh deployment starts a fresh seq space — a leftover
                # cursor would skip or mis-replay events on first connect.
                cursor_path.unlink(missing_ok=True)
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

    def _local_port(url: str) -> int | None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return None
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    if not es_url:
        es_port = 8080
        es_url = f"http://localhost:{es_port}"
        result = ensure_running(es_port, project_path=project_path)
        if result == "started":
            log.info("No event server configured — started local server on port %d", es_port)
        elif result == "connected":
            log.info("Connected to existing local event server on port %d", es_port)
        es_deployment, es_key = _register_with_retry(es_url)
    elif (es_port := _local_port(es_url)) is not None:
        result = ensure_running(es_port, project_path=project_path)
        if result == "started":
            log.info("Configured local event server started on port %d", es_port)
        elif result == "connected":
            log.info("Connected to configured local event server on port %d", es_port)
        if not (es_deployment and es_key):
            es_deployment, es_key = _register_with_retry(es_url)
        elif not bubble_state_path(project_path).exists():
            log.info("Saved deployment but no bubble.json — pre-bubble upgrade, re-registering")
            cursor_path.unlink(missing_ok=True)
            es_deployment, es_key = _register_with_retry(es_url)
        else:
            try:
                _bubble = ensure_bubble(es_url, project_path)
                if has_external:
                    try:
                        register_slack_workspaces(
                            es_url, cfg,
                            bubble_id=_bubble["bubble_id"],
                            bubble_key=_bubble["bubble_key"],
                        )
                    except Exception as e:
                        log.info("Signed Slack registration unavailable (%s) — unsigned", e)
                        register_slack_workspaces(es_url, cfg)
                authorized = authorize_resources(
                    es_url, cfg, subscribe,
                    _bubble["bubble_id"], _bubble["bubble_key"],
                    filter_unauthorized=False,
                )
                from bobi import http as pooled
                resp = pooled.put(
                    f"{es_url}/deployments/{es_deployment}/subscriptions",
                    json={"replace": authorized},
                    headers={
                        "Authorization": f"Bearer {es_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
            except Exception as e:
                log.warning("Subscription sync failed, re-registering: %s", e)
                cursor_path.unlink(missing_ok=True)
                es_deployment, es_key = _register_with_retry(es_url)
    elif not (es_deployment and es_key):
        # No saved deployment for this session — register fresh rather
        # than PUT to a guaranteed-400 empty deployment URL.
        es_deployment, es_key = _register_with_retry(es_url)
    elif not bubble_state_path(project_path).exists():
        # Pre-bubble upgrade: saved deployment_state from a version that
        # predates auth bubbles. The old api_key can't sign publishes
        # against a v0.21+ server → 403. Drop the stale state and
        # re-register through ensure_bubble to mint/join a bubble.
        log.info("Saved deployment but no bubble.json — pre-bubble upgrade, re-registering")
        cursor_path.unlink(missing_ok=True)
        es_deployment, es_key = _register_with_retry(es_url)
    else:
        # This session restarting with its own saved deployment — sync any
        # new subscription keys onto it. Never PUT to another session's
        # deployment; state is per-session by construction. Authorize resource
        # grants first (#488) so a global topic added here is not hard-rejected;
        # a github/linear topic we can't authorize is dropped from the PUT.
        try:
            _bubble = ensure_bubble(es_url, project_path)
            if has_external:
                try:
                    register_slack_workspaces(
                        es_url, cfg,
                        bubble_id=_bubble["bubble_id"], bubble_key=_bubble["bubble_key"],
                    )
                except Exception as e:
                    log.info("Signed Slack registration unavailable (%s) — unsigned", e)
                    register_slack_workspaces(es_url, cfg)
            authorized = authorize_resources(
                es_url, cfg, subscribe,
                _bubble["bubble_id"], _bubble["bubble_key"],
                filter_unauthorized=False,
            )
        except Exception as e:
            log.info("Pre-PUT resource authorization unavailable (%s)", e)
            authorized = subscribe
        from bobi import http as pooled
        try:
            resp = pooled.put(
                f"{es_url}/deployments/{es_deployment}/subscriptions",
                json={"replace": authorized},
                headers={
                    "Authorization": f"Bearer {es_key}",
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            resp.raise_for_status()
        except Exception as e:
            log.info("Subscription update failed (%s) — re-registering", e)
            es_deployment, es_key = _register_with_retry(es_url)

    # Note: Slack-bot registration (signed, also writing the #487 outbound record
    # and the #488 slack grant) now happens in `_authorize_subscriptions` BEFORE
    # register/PUT, so a `slack:` subscription has its grant by the time the
    # server checks it. The auto-dispatch reactor (also has_external) is wired
    # below, after the client connects.

    # Dedicated queue per session: multiple clients can live in one process
    # (sequential workflow phases), and a shared queue would let one session's
    # drain steal and drop another's events.
    from queue import SimpleQueue
    session_queue: SimpleQueue = SimpleQueue()

    def _resubscribe_on_deaf() -> None:
        """Re-assert subscriptions after the client force-reconnects a deaf path.

        A zombie socket is healed by the reconnect itself; this additionally
        repairs a stale server-side subscription index (e.g. the deployment was
        dropped from the index during a long redeploy gap) by re-adding every
        key. Idempotent — the server dedups keys already present (#425).
        """
        from bobi import http as pooled
        pooled.put(
            f"{es_url}/deployments/{es_deployment}/subscriptions",
            json={"replace": subscribe},
            headers={
                "Authorization": f"Bearer {es_key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    client = EventServerClient(
        server_url=es_url,
        deployment_id=es_deployment,
        api_key=es_key,
        cursor_path=cursor_path,
        queue=session_queue,
        on_deaf_reconnect=_resubscribe_on_deaf,
    )
    client.start()

    # Build auto-dispatch reactor from config (if rules are defined).
    reactor = None
    if has_external and cfg.auto_dispatch:
        from bobi.events.reactor import EventReactor
        # Resolve the bot's own GitHub login so the reactor can skip
        # auto-dispatching on the bot's own events (issue #411). Self-author
        # skip is the default, so we need the login unless EVERY rule opts back
        # in via allow_self_authored.
        self_login = None
        if any(not r.get("allow_self_authored") for r in cfg.auto_dispatch):
            self_login = _resolve_self_github_login()
        reactor = EventReactor.from_config(
            cfg.auto_dispatch, cwd=str(project_path), self_login=self_login)
        log.info("Auto-dispatch reactor loaded with %d rule(s) (self_login=%s)",
                 len(reactor.rules), self_login or "unresolved")

    drain_thread = threading.Thread(
        target=drain_loop, args=(session_name,),
        kwargs={"reactor": reactor, "queue": session_queue,
                "cursor_ack": client.ack_through},
        daemon=True, name="agent-drain",
    )
    drain_thread.start()
    log.info(f"Event subscription started for {session_name}: {subscribe}")

    return Subscription(client=client, drain_thread=drain_thread, queue=session_queue)


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
    input_fields = args.get("input_fields", {})

    from bobi.paths import bind_root, bobi_root
    # The spawner tells the child its installation root — identity is
    # inherited, never inferred from cwd, so it survives repos that live
    # outside the installation tree. A blob without a root is a spawner
    # bug; failing loudly here beats guessing. cwd stays the working dir.
    if "root" not in args:
        raise RuntimeError(
            "spawn args blob has no 'root' — the spawning process is running "
            "older code than what is installed on disk. Restart the manager "
            "after upgrading, then re-dispatch."
        )
    bind_root(Path(args["root"]))
    project_root = bobi_root()
    # The root must be a real runtime: state/sessions writes below would
    # otherwise mkdir a fresh state tree at a bogus path.
    from bobi.paths import agent_yaml_path
    if not agent_yaml_path().is_file():
        raise RuntimeError(
            f"spawn args root {project_root} is not a Bobi installation "
            f"(no package/agent.yaml) — refusing to run with an unverified "
            f"identity."
        )
    from bobi.brain import BRAIN_ENV
    brain_kind = _configured_brain_kind(project_root, os.environ)
    if brain_kind:
        os.environ[BRAIN_ENV] = brain_kind
    else:
        os.environ.pop(BRAIN_ENV, None)

    # Subscription is owned by the Session now: every Session subscribes to
    # inbox/<self> on start, and extra topics (the persistent agent's
    # --subscribe list) flow in via the Session's `subscribe` argument. The
    # workflow path's phase Sessions each self-subscribe to their own inbox.
    if persistent:
        spawn_adhoc(
            cwd=cwd,
            task=task,
            timeout=timeout,
            name=run_key,
            requested_by=requested_by,
            persistent=True,
            role=role,
            subscribe=subscribe,
        )
        return

    from bobi.workflow.orchestrator import run_workflow
    from bobi.workflow.triggers import WorkflowDispatcher

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
        input_fields=input_fields,
    )


# ---------------------------------------------------------------------------
# Non-interactive check execution (background monitor path)
# ---------------------------------------------------------------------------

CHECK_TIMEOUT = 600  # monitor checks are short-lived
CHECK_MAX_TURNS = 8  # cap poll cost — a single check can't balloon into 200 turns


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
        "Use finding=false when everything is healthy and nothing needs attention. "
        "When reporting a finding, include a stable identifier for the underlying "
        'condition as a "key" field inside details (e.g. an email message id, PR '
        "number, or URL). The scheduler deduplicates findings by that key across "
        "repeated checks — do NOT try to deduplicate yourself or suppress a "
        "finding because it may have been reported before; report exactly what "
        "you observe right now."
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


def _parse_check_verdict(text: str) -> dict | None:
    """Return the trailing JSON verdict object a check agent emitted, or None.

    None means the agent produced NO parseable verdict. That is NOT the same as
    a healthy ``{"finding": false}`` — the agent must state finding=false
    explicitly. A missing verdict means the run was malformed or truncated
    (e.g. the model emitted a tool call as literal text and then stopped), i.e.
    an indeterminate check that should be retried, never silently treated as
    "nothing found".
    """
    if not text:
        return None
    # Prefer the last parseable object that actually looks like a verdict.
    for chunk in reversed(_extract_json_objects(text)):
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "finding" in parsed:
            return parsed
    return None


def _parse_check_output(text: str) -> tuple[bool, str, dict]:
    """Extract the trailing JSON verdict as (finding, summary, details).

    Back-compat shim over _parse_check_verdict: defaults to (False, "", {})
    when no verdict is present. Callers that must distinguish a missing verdict
    from an explicit finding=false should use _parse_check_verdict directly.
    """
    verdict = _parse_check_verdict(text)
    if verdict is None:
        return False, "", {}
    finding = bool(verdict.get("finding"))
    summary = str(verdict.get("summary", "")) if finding else ""
    details = verdict.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    return finding, summary, details


def run_check_blocking(
    description: str,
    cwd: str,
    name: str | None = None,
    extra: dict[str, Any] | None = None,
    timeout: int = CHECK_TIMEOUT,
    attempts: int = 2,
) -> CheckResult:
    """Run a one-shot, non-interactive check agent and parse its verdict.

    Reuses the same supervised agent loop as agent phases, but with a
    constrained read-only prompt and no input handler. Blocks until the
    agent finishes or times out.

    A check that errors or produces NO parseable verdict is retried up to
    ``attempts`` times before giving up. An indeterminate run (e.g. a
    transient tool-use glitch where the model emits a tool call as text and
    stops) must NOT be reported as a clean ``finding: false`` — that silently
    drops real signals (a real support email going untriaged) until the next
    interval. Only a genuine verdict — finding true OR an explicit
    finding=false — ends the loop; exhausting all attempts returns
    ``success=False`` so the scheduler treats it as a failed check, not a
    healthy one.
    """
    import hashlib

    short_hash = hashlib.sha256(description.encode()).hexdigest()[:8]
    slug = name or f"check-{short_hash}"
    phase = "check"
    session = _session_name(slug, role="monitor", phase=phase)

    prompt = _build_check_prompt(description, extra)

    registry = get_registry()
    registry.register(SessionEntry(
        name=session, session_id="", role="monitor",
        run_key=slug, title=description[:80], phase=phase,
        cwd=cwd, status="starting",
    ))

    last_error = "check did not run"
    last_result: AgentResult | None = None
    for attempt in range(1, max(1, attempts) + 1):
        # Use a fresh run_key on retry: the supervised runner resumes a saved
        # session id, so reusing the key would replay the botched transcript
        # instead of starting a clean agent turn.
        run_key = slug if attempt == 1 else f"{slug}-retry{attempt}"
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    _run_agent_supervised(prompt, cwd, run_key, phase, timeout,
                                         role="monitor", max_turns=CHECK_MAX_TURNS),
                    timeout=timeout,
                )
            )
        except asyncio.TimeoutError:
            registry.update(session, status="error")
            last_error = f"timeout after {timeout}s"
            log.warning(f"Check '{slug}' attempt {attempt}/{attempts}: {last_error}")
            continue

        last_result = result
        if not result.success:
            last_error = result.error or "check agent failed"
            log.warning(f"Check '{slug}' attempt {attempt}/{attempts} failed: {last_error}")
            continue

        verdict = _parse_check_verdict(result.final_text)
        if verdict is None:
            last_error = ("check produced no parseable verdict — likely a "
                          "malformed tool call or truncated output")
            log.warning(f"Check '{slug}' attempt {attempt}/{attempts}: {last_error}")
            continue

        finding = bool(verdict.get("finding"))
        summary = str(verdict.get("summary", "")) if finding else ""
        details = verdict.get("details") or {}
        if not isinstance(details, dict):
            details = {}
        return CheckResult(
            success=True, finding=finding, summary=summary, details=details,
            raw_output=result.final_text, duration_ms=result.duration_ms,
            total_cost_usd=result.total_cost_usd,
        )

    # Exhausted attempts without a clean verdict — indeterminate, not healthy.
    registry.update(session, status="error")
    return CheckResult(
        success=False, error=last_error,
        raw_output=last_result.final_text if last_result else "",
        duration_ms=last_result.duration_ms if last_result else 0,
        total_cost_usd=last_result.total_cost_usd if last_result else 0.0,
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
            continue  # managers are shown separately in `bobi agent <name> status`
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
