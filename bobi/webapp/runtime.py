"""The ``TeamRuntime`` seam between webapp handlers and team operations.

#690 stage 1: the HTTP handlers in ``server.py`` speak only to a
``TeamRuntime``; the local app binds ``LocalRuntime`` at build time. A hosted
deployment binds a different implementation of the same interface - one
webapp codebase, two deployments, each with a fixed binding. There is no
mode-switching logic.

``LocalRuntime`` operates on this machine's ``$BOBI_HOME/agents/`` tree via
explicit-root service calls. It never binds the process to a runtime root, so
one process serves any number of teams concurrently.

Methods return plain machine-readable data (dicts/lists ready for JSON) and
raise the typed errors below; mapping to HTTP status codes stays in the
handlers.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from bobi import paths
from bobi.chat_history import read_chat, read_transcript_messages

DEFAULT_CHAT_TIMEOUT = 300

def _clean_event_text(text: str) -> str:
    lines = text.strip().splitlines()
    content_lines = []
    in_meta = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Event:"):
            continue
        if re.match(r"^(?:conversation|channel|user_id|repo|action|sender|thread_ts|event_ts|channel_name|chat_id|thread_id):\s*", stripped):
            in_meta = True
            continue
        if not in_meta and stripped:
            content_lines.append(stripped)
    return "\n".join(content_lines).strip()


def _clean_user_message(raw: str) -> str:
    if not raw:
        return ""
    # Filter out system prompts and agent bootstrap instructions
    if raw.lstrip().startswith(("You are an agent in a bobi deployment", "You are a bobi ", "# Bobi Agent", "How you receive events")):
        parts = re.split(r"(?m)^(?=Event:\s*[\w\-\./]+)", raw)
        if len(parts) > 1:
            last_part = parts[-1].strip()
            return _clean_event_text(last_part)
        return ""

    if "Event:" in raw:
        return _clean_event_text(raw)

    # Detect workflow background execution prompts and extract the concise task objective
    if "Workflow " in raw and ("background for run" in raw or "steps:" in raw):
        m_step = re.search(r"Workflow `?[^`\n]+`? steps:[^\n]+\n\n([\s\S]+)$", raw)
        if m_step and m_step.group(1).strip():
            return m_step.group(1).strip()
        m_task = re.search(r"(?m)^\s*task:\s*([^\n]+(?:\n\s{4,}[^\n]+)*)", raw)
        if m_task:
            return re.sub(r"\s+", " ", m_task.group(1).strip())

    return raw.strip()


def _internal_activity_title(text: str, trigger: str) -> str | None:
    from bobi.history import _is_sleep_cycle_task

    if _is_sleep_cycle_task(text):
        return "Sleep Cycle & Memory Compaction"
    if trigger in {"user", "chat", "direct", "slack", "slack/message"}:
        return None
    if re.match(r"(?is)^\s*(?:compaction(?: required| requested| notice)?|compact(?:ing)? (?:the )?(?:session|conversation|context))\b", text):
        return "Sleep Cycle & Memory Compaction"
    if re.match(r"(?is)^\s*(?:no (?:new |pending )?events\b|(?:idle|waiting)\s*[:.-]?\s*(?:no events|for events))", text):
        return "Idle Standby (No new events)"
    if re.match(r"(?is)^\s*(?:process(?:ing)? (?:the |pending |incoming )?inbox\b|check(?:ing)? (?:the )?inbox\b)", text):
        return "Inbox Event Processing"
    if text.lstrip().startswith((
        "You are an agent in a bobi deployment", "You are a bobi ",
        "# Bobi Agent", "How you receive events",
    )):
        return "Agent Startup & Initialization"
    return None

def _turn_conversation(turn: dict, entries: list[dict], next_started: int | None = None,
                       session: dict | None = None) -> dict:
    end = turn.get("ended_at_us")
    if end is None and next_started is None and turn.get("status") != "running":
        entries = []
    selected = []
    for entry in entries:
        try:
            at = datetime.fromisoformat(entry.get("at", "").replace("Z", "+00:00"))
            if at.tzinfo is None:
                continue
            at_us = int(at.timestamp() * 1_000_000)
        except (ValueError, TypeError, OverflowError):
            continue
        if at_us < turn["started_at_us"] or (end is not None and at_us > end):
            continue
        if next_started is not None and at_us >= next_started:
            continue
        selected.append(entry)
    inputs = [entry["text"] for entry in selected if entry["kind"] == "message" and entry["role"] == "user"]
    responses = [entry["text"] for entry in selected if entry["kind"] == "message" and entry["role"] == "agent"]
    prompt = "\n\n".join(inputs)
    response = "\n\n".join(responses)
    prompt_bytes = prompt.encode("utf-8")
    response_bytes = response.encode("utf-8")
    user_messages = []
    internal_titles = []
    trigger = turn.get("trigger_kind") or "agent"
    for text in inputs:
        internal_title = _internal_activity_title(text, trigger)
        cleaned = _clean_user_message(text)
        if cleaned and (internal_title is None or (internal_title == "Agent Startup & Initialization" and "Event:" in text)):
            user_messages.append(cleaned)
        elif internal_title:
            internal_titles.append(internal_title)
    user_message = "\n\n".join(user_messages)
    if not user_message and any(_internal_activity_title(text, "agent") == "Idle Standby (No new events)" for text in responses):
        internal_titles.append("Idle Standby (No new events)")
    context = session or turn
    session_name = context.get("session_name") or ""
    sleep_cycle = context.get("role") == "curator" or session_name.startswith("curator-") or trigger in {"sleep_cycle", "compaction"}
    if not user_message and sleep_cycle and (not internal_titles or set(internal_titles) == {"Agent Startup & Initialization"}):
        internal_titles.append("Sleep Cycle & Memory Compaction")
    semantic_title = re.sub(r"\s+", " ", user_message).strip()[:160] if user_message else next(
        (title for title in ("Sleep Cycle & Memory Compaction", "Idle Standby (No new events)",
                             "Inbox Event Processing", "Agent Startup & Initialization") if title in internal_titles),
        "Sleep Cycle & Memory Compaction" if sleep_cycle else "Agent Startup & Initialization" if trigger == "startup"
        else "Inbox Event Processing" if trigger == "inbox" else "Agent activity",
    )
    origin = {"source": turn.get("trigger_kind") or "agent"}
    reference = re.search(r"(?m)^\s*conversation:\s*(\S+)", prompt)
    if reference:
        from bobi.conversation import parse_conversation

        conversation = parse_conversation(reference[1])
        if conversation and conversation.source == "slack":
            channel = re.search(r"(?m)^\s*channel_name:\s*([^\n]+)", prompt)
            origin = {"source": "slack", "channel_id": conversation.chat_id,
                      "channel_name": channel[1].strip() if channel else None, "thread_id": conversation.thread_id,
                      "url": None}
            if (re.fullmatch(r"T[A-Z0-9]+", conversation.scope)
                    and re.fullmatch(r"[CDG][A-Z0-9]+", conversation.chat_id)
                    and re.fullmatch(r"\d+\.\d+", conversation.thread_id)):
                origin["url"] = (f"https://app.slack.com/client/{conversation.scope}/{conversation.chat_id}"
                                 f"/thread/{conversation.chat_id}-{conversation.thread_id}")
    preview = []
    preview_bytes = 0
    from bobi.chat_history import tool_result_preview

    for entry in selected[:200]:
        text = entry.get("text", "")
        entry = dict(entry)
        entry.setdefault("total_bytes", len(text.encode("utf-8")))
        if entry["kind"] == "tool_result":
            entry["text"] = tool_result_preview(text)
            if len(entry["text"]) < len(text):
                entry["truncated"] = True
        elif len(text.encode("utf-8")) > 32768:
            entry["text"] = text.encode("utf-8")[:32768].decode("utf-8", errors="ignore")
            entry["truncated"] = True
        preview_bytes += len(entry["text"].encode("utf-8"))
        if preview_bytes > 262144:
            break
        preview.append(entry)
    return {"status": "matched" if inputs and responses else "partial" if selected else "unavailable",
            "input": prompt_bytes[:32768].decode("utf-8", errors="ignore") or None,
            "user_message": user_message.encode("utf-8")[:32768].decode("utf-8", errors="ignore") or None,
            "semantic_title": semantic_title,
            "response": response_bytes[:32768].decode("utf-8", errors="ignore") or None,
            "truncated": len(prompt_bytes) > 32768 or len(response_bytes) > 32768 or len(preview) < len(selected)
                         or any(entry.get("truncated") for entry in preview),
            "entries": preview, "origin": origin,
            "prompt_snippet": user_message[:60] or None, "response_snippet": response[:60] or None}


# --- Errors -----------------------------------------------------------------

class TeamRuntimeError(Exception):
    """Base for runtime-surface errors the handlers translate to HTTP."""


class UnknownTeam(TeamRuntimeError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown agent '{name}'")


class UnknownRun(TeamRuntimeError):
    """No run with that id under this team - a 404, not a lifecycle failure.

    Separate from ``UnknownTeam`` because the team resolved fine; it is the
    run inside it that is gone, which is the ordinary case once a record ages
    past the retention cap while a browser still holds its row.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"unknown run '{run_id}'")


class TeamAlreadyRunning(TeamRuntimeError):
    def __init__(self, pid: int) -> None:
        self.pid = pid
        super().__init__(f"already running (pid {pid})")


class TeamPreflightFailed(TeamRuntimeError):
    def __init__(self, report: str) -> None:
        self.report = report
        super().__init__("preflight failed")


class TeamDidNotStop(TeamRuntimeError):
    def __init__(self, pid: int) -> None:
        self.pid = pid
        super().__init__("manager did not stop")


class TeamLifecycleError(TeamRuntimeError):
    """Any other lifecycle failure; the message is user-facing."""


# --- Interface ----------------------------------------------------------------

class TeamRuntime(ABC):
    """Everything the webapp needs from a fleet of teams.

    The stage-1 surface from #690: dashboard snapshot, per-team status,
    lifecycle, subagent roster, messages/transcripts, and submit-then-poll
    chat. Chat is deliberately two calls (submit returns a message id, the
    outcome lands on the job) so no request is held open for a minutes-long
    agent reply regardless of what transport an implementation uses.

    Widening this ABC: both implementers now live in-tree - ``LocalRuntime``
    below and ``EventBusRuntime`` in ``bobi/webapp/event_bus.py`` - so a new
    ``@abstractmethod`` is caught by this repo's own CI, in the same commit
    that adds it. Add the method and both implementations together; there is
    no longer a sequencing rule to follow, because there is no longer an
    implementer this repo cannot see.

    What that rule protected against, and why it is gone: a private consumer
    carried its own COPY of ``EventBusRuntime`` and tracked this repo's
    ``dev`` channel, so an abstract method here broke it on merge - Python
    rejects instantiating a subclass that has not implemented one. That copy
    is being retired in favour of the in-tree class. Until it is, a consumer
    pinning a RELEASED ``bobi`` is unaffected (it moves when it chooses to);
    one tracking ``dev`` sees the break immediately, which is the accepted,
    announced cost of this change rather than a surprise.

    Keep new methods read-only-safe unless they are deliberate operator
    writes, and document the wire shape in the docstring, as below - both
    runtimes must emit it identically, it is rendered once.
    """

    def metrics(self, name: str, view: str, args: dict) -> dict:
        """Bounded metrics reads; runtimes without local telemetry explicitly refuse."""
        from bobi.metrics.query import MetricsQueryError

        raise MetricsQueryError("metrics are unavailable for this runtime", "metrics_unsupported")

    def get_routing_config(self, name: str) -> dict:
        """Read JEV routing experiment configuration for an agent."""
        from bobi.metrics.query import MetricsQueryError

        raise MetricsQueryError("routing configuration is unavailable for this runtime", "metrics_unsupported")

    def update_routing_config(self, name: str, payload: dict) -> dict:
        """Enable, disable, or update JEV routing configuration for an agent."""
        from bobi.metrics.query import MetricsQueryError

        raise MetricsQueryError("routing configuration is unavailable for this runtime", "metrics_unsupported")

    async def test_route(self, name: str, payload: dict) -> dict:
        """Classify sandbox input without sessions, assignments, or telemetry writes."""
        from bobi.metrics.query import MetricsQueryError

        raise MetricsQueryError("routing simulation is unavailable for this runtime", "metrics_unsupported")

    @abstractmethod
    def dashboard(self) -> dict:
        """Every team slot this runtime can see."""

    @abstractmethod
    def team_status(self, name: str) -> dict:
        """One team's card (installed/running/pid/description)."""

    @abstractmethod
    def start_team(self, name: str) -> dict:
        """Start the team's manager; returns ``{"ok": True, "pid": int}``."""

    @abstractmethod
    def stop_team(self, name: str) -> dict:
        """Stop the team's manager; returns the stop outcome fields."""

    @abstractmethod
    def restart_team(self, name: str) -> dict:
        """Stop then start; returns ``{"ok": True, "pid": int}``."""

    @abstractmethod
    def subagents(self, name: str) -> list[dict]:
        """The team's session roster, manager first."""

    @abstractmethod
    def messages(self, name: str, session: str) -> list[dict]:
        """A session's transcript messages (chat-log fallback)."""

    @abstractmethod
    def transcript(self, name: str, session: str) -> dict:
        """A session's transcript as a DEBUGGING view: timestamped lines,
        tool calls included.

        Shape::

            {"session": str,
             "entries": [{"kind",     # message | tool | tool_result
                          "role",     # user | agent
                          "text", "at",        # ISO 8601, "" if unrecorded
                          "tool",              # "" unless a tool line
                          "truncated",         # a clipped tool result
                          "is_error"}, ...],
             "usage": {"started_at", "ended_at", "tokens", "cost_usd",
                       "status"}}

        Distinct from ``messages`` on purpose, and ``messages`` is unchanged:
        that is the CHAT view (two roles, prose only), which is what a
        conversation panel wants. Debugging a run wants when each turn
        happened and what the agent DID between saying things. Both read the
        same transcript; they differ in what they discard.

        ``at`` is empty where the on-disk format records no per-entry
        timestamp (Codex rollouts), rather than synthesized. ``usage`` feeds
        the slab header.
        """

    def system_logs(self, name: str, lines: int = 200) -> dict:
        """Recent daemon system log lines (from manager.log) and health error detection."""
        return {"ok": True, "logs": [], "total_lines": 0}

    @abstractmethod
    def run_details(self, name: str, run_id: str) -> dict:
        """What to show for a run that has no transcript to open.

        Shape (see ``bobi.webapp.details.build_details``)::

            {"kind": "monitor",
             "run": {...},          # the run record, whole
             "definition": {...},   # the monitor's definition, {} if gone
             "session_id": str}     # "" when the firing spawned nothing

        A `$0` cached monitor tick and a firing that failed before its agent
        started both have a record and no session. They get the record plus
        the definition of the monitor that produced it - what happened, beside
        what it was asked to do.

        Raises ``UnknownRun`` when no record carries that id.
        """

    @abstractmethod
    def chat_submit(self, name: str, session: str, text: str) -> str:
        """Queue *text* for *session*; returns a message id to poll."""

    @abstractmethod
    def chat_job(self, name: str, message_id: str) -> dict | None:
        """The submit's outcome: pending/done/error. None when the id is
        unknown or belongs to a different team."""

    # --- observability (read-only, #733) --------------------------------
    # Surfaces signals we already capture; no new emitters. One interface,
    # both runtimes, rendered once. The spend vertical folds the per-session
    # cost already written to each session's state.json.

    @abstractmethod
    def spend_summary(self, name: str) -> dict:
        """One team's spend: total plus breakdown by session, role, and model.

        Shape (see ``bobi.costs.CostSummary.to_dict``)::

            {"total_cost_usd", "sessions_counted",
             "by_provider", "by_model", "by_session", "by_role",
             "estimated_cost_usd", "estimated_by_model", "tokens_by_model"}

        ``total_cost_usd``/``by_*`` are provider-reported dollars only;
        ``estimated_*`` is fold-time list-price math over recorded token
        counts for models that report no dollars (#760), kept separate so an
        estimate is never mistaken for a bill. ``tokens_by_model`` carries the
        raw token volumes - the render fallback when no estimate exists.

        Implementations may add ``script_cache`` (see
        ``bobi.webapp.savings``): what the cached-script monitor runner did
        NOT spend. Those are counterfactual dollars and live in their own
        block for that reason - they are never summed into the recorded or
        estimated totals above.
        """

    @abstractmethod
    def overview(self, name: str) -> dict:
        """What a team IS: its description, roles, reach, automations, brain,
        and spend cap - the identity header's read-only view of composition.

        Shape (see ``bobi.webapp.overview.build_overview``)::

            {"name", "description",
             "roles": [{"name", "description"}, ...],
             "chat": {"service", "channels"},
             "services": [{"name", "events", "required"}, ...],
             "automations": {"monitors", "paused_monitors", "workflows"},
             "brain": {"kind", "model", "effort", "max_turns", "gateway"},
             "spend_cap": {"value", "is_default"},
             "entry_role": str}

        Read-only by design: composition is edited in setup, and this exists
        so nobody has to open setup to remember what a team is. Values come
        from the installed package image, never a source directory - the
        runtime runs the image, so the image is the truth.
        """

    @abstractmethod
    def fleet_spend(self) -> dict:
        """Fleet-wide spend for the dashboard: a total plus per-team totals::

            {"total_cost_usd", "estimated_cost_usd", "sessions_counted",
             "teams": [{"name", "total_cost_usd", "estimated_cost_usd",
                        "sessions_counted"}, ...]}

        Same recorded/estimated separation as ``spend_summary``.
        """

    @abstractmethod
    def health_summary(self, name: str) -> dict:
        """One team's system health: manager liveness, session statuses, and
        (where a supervisor records one) the recent lifecycle trail.

        Shape::

            {"reachability": "live" | "stale" | "unreachable",
             "last_heartbeat_at": str | None,
             "state": "running" | "stopped" | "not_responding",
             "detail": str,          # one line of prose for a human
             "segments": [{"key", "label", "kind", "value", "note"}, ...],
             "manager": {"status", "pid", "running", "healthy",
                         "restart_count", "last_restart_reason",
                         "last_restart_at", "idle_seconds"},
             "sessions": [{"name", "role", "status"}, ...],
             "lifecycle": [{"event", "received_at", "at", ...}, ...]}

        ``lifecycle`` is newest first. Fields a runtime cannot know are
        null/empty rather than omitted (a local team has no heartbeats and no
        supervisor trail), so render code branches on value, never on key
        presence.

        ``state`` is the agent's own state, and ``not_responding`` is why it
        exists: a manager process can be alive while the manager is not
        working, and reporting that as running is the failure this surface
        cannot afford. ``segments`` is the status strip's telemetry, ordered
        for display and BEST-EFFORT: a reading this runtime cannot produce is
        omitted rather than faked, so callers must render the list they are
        given rather than expecting a fixed set. ``kind`` says how to read
        ``value`` - ``duration`` (elapsed seconds), ``time`` (epoch seconds),
        ``count``, ``text`` - and formatting belongs to the client, which
        knows the viewer's timezone.

        These three keys are newer than this interface. An implementation
        that predates them may omit them; ``bobi.webapp.health.normalize``
        completes the payload at the response boundary so the null/empty rule
        above holds for every runtime. See ``bobi/webapp/health.py``.
        """

    @abstractmethod
    def session_log(self, name: str) -> dict:
        """One team's session history, newest first: every session the
        registry still holds - active and terminal - with its honest outcome.

        Shape (each row is the ``serialize_session`` view: the roster card
        fields plus ``session_id``/``error``/``terminal_at``)::

            {"sessions": [{"name", "session_id", "role", "title", "phase",
                           "project", "status",  # starting|running|idle|
                                                 # completed|failed|crashed
                                                 # (+ "done" = completed,
                                                 #  "error" = failed)
                           "error",              # terminal failure message,
                                                 # "" otherwise
                           "ended",              # bool: status has left the
                                                 # active vocabulary
                           "model", "provider", "total_cost_usd", "run_key",
                           "started_at", "last_activity",  # epoch seconds
                           "terminal_at",        # epoch seconds | None
                           "is_manager"}, ...],
             "counts": {"active", "completed", "failed", "crashed"},
             "truncated": bool}

        ``sessions`` is newest first by last activity. ``counts`` covers the
        whole history even when an implementation caps ``sessions`` for
        transport (``truncated`` flags that cap). Transcripts drill in via
        ``messages(name, session)`` - a terminal session's transcript stays
        readable as long as its registry entry exists.
        """

    @abstractmethod
    def runs(self, name: str, *, status: str = "", kind: str = "", query: str = "",
             offset: int = 0, limit: int | None = None) -> dict:
        """One team's runs: sessions, workflow runs, and monitor runs merged
        into one list, newest first with live runs at the top.

        Shape (see ``bobi.webapp.runs.build_runs``)::

            {"runs": [{"kind",        # session | workflow | monitor
                       "key",         # stable row identity
                       "status",      # running|idle|done|failed|crashed|
                                      # awaiting_action|closed
                       "title", "origin",       # what it is, what kicked it off
                       "started_at",            # ISO 8601, "" if unknown
                       "duration_seconds",      # float | None
                       "tokens", "cost_usd", "est_cost_usd",
                       "error",                 # "" unless it failed
                       "session_id", "run_id",  # "" when the row has neither
                       "detail"}, ...],         # kind-specific extras
             "counts": {"all", "running", "awaiting_action", "failed"},
             "total": int, "offset": int, "limit": int, "query": str,
             "truncated": bool}

        ``status`` and ``query`` filter before ``offset`` / ``limit`` paginate;
        ``failed`` covers terminal failures; human gates use
        ``awaiting_action``.
        ``counts`` describes the whole set and ``total`` the filtered set.
        """

    @abstractmethod
    def resume_run(self, name: str, run_id: str, *, verdict: str = "",
                   reply: str = "") -> dict:
        """Resume one suspended workflow run, answering its gate.

        ``verdict`` is one of ``bobi.workflow.schema.GATE_VERDICTS`` and
        ``reply`` is the human's own words. Both reach the workflow as the
        ``event`` scope, so a route step after the await takes the branch the
        answer chose. An unrecognised verdict raises ``TeamLifecycleError``
        (409) instead of resuming; an absent one is not an approval.

        Returns
        ``{"ok", "accepted", "run_id", "workflow", "await_event", "verdict"}``.
        ``accepted`` is the honest word: a workflow run takes as long as it
        takes, so this returns once the resume is under way and the caller
        watches ``runs`` for the status to move - the same submit-then-poll
        discipline chat uses. No request is ever held open for a workflow.

        Raises ``UnknownRun`` when no run carries that id, and
        ``TeamLifecycleError`` (409) when the run is not in a resumable
        state. Resuming is single-winner: exactly one resume of a given run
        proceeds even if two arrive together.
        """

    @abstractmethod
    def remind_run(self, name: str, run_id: str) -> dict:
        """Resend a waiting workflow's user-facing gate notification.

        Returns ``{"ok", "delivered", "run_id", "workflow", "await_event"}``.
        The run is untouched - this nudges the human, it does not advance the
        workflow. Same error vocabulary as ``resume_run``, plus
        ``TeamLifecycleError`` when the notification could not be delivered.
        """

    @abstractmethod
    def close_run(self, name: str, run_id: str) -> dict:
        """Close a waiting workflow without advancing its gate.

        Returns ``{"ok", "closed", "run_id", "workflow"}``, and marks the
        run's session cancelled. Same error vocabulary as ``resume_run``.
        """


# --- Local implementation ----------------------------------------------------

def _first_paragraph(md: str) -> str:
    """First prose paragraph of an agent.md - the card description."""
    para: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            if para:
                break
            continue
        para.append(s)
    return " ".join(para)


def _describe(agent_dir: Path) -> str:
    try:
        return _first_paragraph((agent_dir / "agent.md").read_text())[:160]
    except OSError:
        return ""


def _manager_pid(root: Path) -> int:
    """The manager pid when alive, else 0. A pure filesystem+signal check -
    the dashboard read path never touches a runtime bind."""
    from bobi.sdk import pid_alive, read_pid

    pid = read_pid(paths.manager_pid_path(root))
    return pid if pid > 0 and pid_alive(pid) else 0


def agent_card(name: str) -> dict:
    """One dashboard card: an installed agent slot and its runtime state."""
    root = paths.agent_run_root(name)
    pid = _manager_pid(root)
    return {
        "name": name,
        "installed": True,
        "running": bool(pid),
        "pid": pid,
        "description": _describe(paths.package_dir(root)),
    }


def design_card(name: str) -> dict:
    """A source-only slot (designed, never installed) - dashboard shows it so
    the library and the runtime roster share one home."""
    return {
        "name": name,
        "installed": False,
        "running": False,
        "pid": 0,
        "description": _describe(paths.agent_source_dir(name)),
    }


def session_usage(root: Path, session: str) -> dict:
    """The transcript slab header's duration/tokens/cost for one session.

    Module-level, like the serializers below, because the supervisor answers
    the hosted `transcript` command from the box and must produce the SAME
    header the local UI does. An unknown session yields the zero envelope
    rather than raising: the entries are the payload, and a transcript that
    outlived its registry entry still renders.
    """
    from bobi.sdk import SessionRegistry

    entry = SessionRegistry(root).get(session)
    if entry is None:
        from bobi.webapp.runs import _metrics_usage_by_session
        t_usage = _metrics_usage_by_session(root).get(session)
        if t_usage:
            return {
                "started_at": 0.0, "ended_at": 0.0,
                "tokens": t_usage.get("total_tokens", 0),
                "cost_usd": t_usage.get("cost_usd", 0.0),
                "status": "",
            }
        return {"started_at": 0.0, "ended_at": 0.0, "tokens": 0,
                "cost_usd": 0.0, "status": ""}
    tokens = sum(
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("output_tokens", 0) or 0)
        for u in (entry.model_usage or {}).values()
        if isinstance(u, dict))
    cost_usd = round(entry.total_cost_usd or 0.0, 6)
    if tokens == 0 and cost_usd == 0.0:
        from bobi.webapp.runs import _metrics_usage_by_session
        t_usage = _metrics_usage_by_session(root).get(session)
        if t_usage:
            tokens = t_usage.get("total_tokens", 0)
            cost_usd = t_usage.get("cost_usd", 0.0)
    return {
        "started_at": entry.started_at or 0.0,
        "ended_at": entry.terminal_at or 0.0,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "status": entry.status,
    }


def serialize_subagent(entry, *, manager_name: str = "") -> dict:
    """A session's card view. Mirrors the fields a `SessionEntry`
    (`bobi.sdk.SessionEntry`) exposes; `is_manager` flags the entry-point
    session so the UI can badge it."""
    return {
        "name": entry.name,
        "role": entry.role,
        "title": entry.title,
        "phase": entry.phase,
        "project": entry.project,
        "status": entry.status,
        "model": entry.model,
        "provider": entry.provider,
        "total_cost_usd": round(entry.total_cost_usd or 0.0, 4),
        "run_key": entry.run_key,
        "started_at": entry.started_at,
        "last_activity": entry.last_activity,
        "is_manager": bool(manager_name) and entry.name == manager_name,
    }


def ordered_subagents(entries, *, manager_name: str = "") -> list:
    return sorted(entries,
                  key=lambda e: (0 if manager_name and e.name == manager_name
                                 else 1, e.started_at or 0))


def serialize_session(entry, *, manager_name: str = "") -> dict:
    """A session-log row (#733 vertical 3): the roster card view plus the
    honest terminal outcome. ``status`` already carries the MDS-65 vocabulary
    (completed/failed/crashed); ``error``/``terminal_at`` say why and when.
    ``ended`` derives from the ACTIVE vocabulary (not the terminal one) so
    render code never has to enumerate every word a writer may record -
    stopped/cancelled/legacy words are all honestly "over". The hosted
    supervisor builds the identical row."""
    from bobi.sdk import ACTIVE_STATUSES

    row = serialize_subagent(entry, manager_name=manager_name)
    row["session_id"] = entry.session_id
    row["error"] = entry.error
    row["terminal_at"] = entry.terminal_at or None
    row["ended"] = entry.status not in ACTIVE_STATUSES
    return row


def session_outcome_counts(entries) -> dict:
    """Outcome buckets over a team's whole history. Legacy ``done`` records
    (pre-MDS-65 successes) count as completed; ``error`` counts as failed
    (session.py/subagent.py still write it for turn-level failures -
    rotation-recovery death, monitor timeouts, unparseable verdicts).
    Statuses outside the vocabulary (stopped/cancelled) stay listed but
    uncounted."""
    from bobi.sdk import (
        ACTIVE_STATUSES,
        TERMINAL_COMPLETED,
        TERMINAL_CRASHED,
        TERMINAL_FAILED,
    )

    counts = {"active": 0, "completed": 0, "failed": 0, "crashed": 0}
    for e in entries:
        if e.status in ACTIVE_STATUSES:
            counts["active"] += 1
        elif e.status in (TERMINAL_COMPLETED, "done"):
            counts["completed"] += 1
        elif e.status in (TERMINAL_FAILED, "error"):
            counts["failed"] += 1
        elif e.status == TERMINAL_CRASHED:
            counts["crashed"] += 1
    return counts


def ordered_session_log(entries) -> list:
    """Session-log order: newest activity first (a log, not a roster)."""
    return sorted(entries, key=lambda e: e.last_activity or 0.0, reverse=True)


_BRAIN_PROVIDER = {"claude": "anthropic", "codex": "openai"}
_PROVIDER_TITLE = {"anthropic": "Anthropic", "openai": "OpenAI", "deepseek": "DeepSeek", "google": "Google"}


def _agent_brain(root: Path, env: dict) -> tuple[str, bool]:
    """The engine an agent runs (``claude`` / ``codex``) and whether it dials a gateway.

    The process env (``BOBI_BRAIN``, ``BOBI_GATEWAY_BASE_URL``) overrides the
    installed ``agent.yaml`` brain block, as at agent start."""
    from bobi.brain import BRAIN_KIND_ALIASES, DEFAULT_BRAIN, normalize_brain_kind
    from bobi.config import Config

    try:
        config = Config.load(root)
    except Exception:
        config = Config()
    kind = env.get("BOBI_BRAIN") or config.brain_kind or DEFAULT_BRAIN
    gateway = bool(env.get("BOBI_GATEWAY_BASE_URL") or config.brain_base_url or kind in BRAIN_KIND_ALIASES)
    return normalize_brain_kind(kind), gateway


def _routing_problems(policy_brain: str, candidates: list[str], brain: str, gateway: bool) -> list[str]:
    """Why a routing policy cannot run on this agent, in operator words.

    Behind a gateway any provider's model can run. A native brain calls one
    vendor, so a routing-prefixed id or another provider's model would fail
    when the session connects."""
    problems = []
    if policy_brain not in ("auto", brain):
        problems.append(f"policy brain {policy_brain} does not match this agent's {brain} brain; use auto")
    native = _BRAIN_PROVIDER.get(brain)
    if gateway or native is None:
        return problems
    from bobi.costs import model_provider

    def an(title: str) -> str:
        return f"{'an' if title[0] in 'AEIOU' else 'a'} {title}"

    vendor = _PROVIDER_TITLE[native]
    for model in candidates:
        if re.match(r"^[a-z]+[/:]", model, re.IGNORECASE):
            problems.append(f"{model} is a gateway routing id, but this agent's {brain} brain calls {vendor} "
                            f"directly; set BOBI_GATEWAY_BASE_URL or use a native {vendor} model id")
        elif (provider := model_provider(model)) and provider != native:
            problems.append(f"{model} is {an(_PROVIDER_TITLE[provider])} model, but this agent's {brain} brain calls "
                            f"{vendor} directly; set BOBI_GATEWAY_BASE_URL to route across providers or pick {an(vendor)} model")
    return problems


class LocalRuntime(TeamRuntime):
    """Today's behavior: this machine's agents tree, explicit-root service
    calls, chat delivered by a background thread per submit."""

    def __init__(self) -> None:
        self._metrics_reads = threading.BoundedSemaphore(4)
        self._routing_tests = threading.BoundedSemaphore(4)
        # Submit-then-poll job store. Carries only status and errors; the
        # reply itself reaches the transcript via the messages poll. Guarded
        # by a lock: submits and finishing worker threads share it.
        self._chat_jobs: dict[str, dict] = {}
        self._chat_lock = threading.Lock()
        # Spawning pins process-global brain state for the in-process
        # preflight probe (service.spawn_team, #655) - serialize starts so
        # two teams' preflights never interleave those env writes. Held only
        # for the spawn call, not for stop waits or chat.
        self._spawn_lock = threading.Lock()

    def _resolve(self, name: str) -> Path:
        try:
            return paths.resolve_root_for_agent(name)
        except RuntimeError:
            raise UnknownTeam(name) from None

    async def test_route(self, name: str, payload: dict) -> dict:
        import asyncio
        import json
        from dataclasses import replace
        from urllib.parse import urlsplit

        from bobi.config import project_env
        from bobi.metrics.features import build_features
        from bobi.metrics.policies.typesafe import TypeSafePolicy
        from bobi.metrics.policy import PolicyError, PolicyRequest, guard_result
        from bobi.metrics.query import MetricsQueryError
        from bobi.metrics.router import ExperimentConfig
        from bobi.redact import redact_secrets

        root = self._resolve(name)
        try:
            if set(payload) - {"prompt", "features", "api_key", "endpoint"}:
                raise ValueError("unsupported simulation fields")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 65536:
                raise ValueError("prompt must contain 1 to 65536 UTF-8 bytes")
            prompt_bytes = len(prompt.encode("utf-8"))
            context = payload.get("features", {})
            if not isinstance(context, dict) or set(context) - {"role", "entry_point", "brain"}:
                raise ValueError("features must contain only role, entry_point, and brain")
            if any(field in payload and not isinstance(payload[field], str) for field in ("api_key", "endpoint")):
                raise ValueError("credential and endpoint overrides must be strings")
            role = context.get("role", "engineer")
            entry_point = context.get("entry_point", "session_start")
            if entry_point not in ("session_start", "subagent_phase", "subagent_persistent",
                                   "subagent_supervised", "workflow_start"):
                raise ValueError("unsupported sandbox entry point")
            if context.get("brain", "claude") not in ("claude", "codex"):
                raise ValueError("unsupported sandbox brain")
            env = project_env(root)
            raw = env.get("BOBI_METRICS_EXPERIMENT_JSON", "").strip()
            if not raw:
                for line in paths.env_path(root).read_text().splitlines() if paths.env_path(root).exists() else []:
                    if line.startswith("# BOBI_METRICS_EXPERIMENT_JSON="):
                        raw = line.split("=", 1)[1].strip()
                        break
            if not raw:
                raise ValueError("configure a JEV policy before simulating routing")
            config = ExperimentConfig.from_mapping(json.loads(raw))
            policy_config = config.policy
            if policy_config is None or policy_config.name != "typesafe-jev":
                raise ValueError("simulation requires a TypeSafe JEV policy")
            if not isinstance(role, str) or role not in {"director", "engineer", "curator", *policy_config.roles}:
                raise ValueError("unsupported sandbox role")
            brain = context.get("brain") or (
                policy_config.brain if policy_config.brain != "auto" else _agent_brain(root, env)[0])
            options = json.loads(policy_config.options_json)
            endpoint = payload.get("endpoint") or options.get("endpoint", "https://api.typesafe.ai/v1/systemone")
            if not isinstance(endpoint, str):
                raise ValueError("invalid simulation endpoint")
            configured_endpoint = urlsplit(options.get("endpoint", "https://api.typesafe.ai/v1/systemone"))
            override = urlsplit(endpoint)
            if (override.hostname, override.port or 443) != (configured_endpoint.hostname, configured_endpoint.port or 443):
                raise ValueError("endpoint override must use the configured TypeSafe host and port")
            options["endpoint"] = endpoint
            policy_config = replace(policy_config, options_json=json.dumps(options))
            key = payload.get("api_key") or env.get(policy_config.credential_env, "")
            if not isinstance(key, str) or len(key) > 4096 or any(char in key for char in "\r\n"):
                raise ValueError("invalid simulation credential")
            policy = TypeSafePolicy(policy_config, api_key=key)
            secrets = tuple(value for value in (key, env.get(policy_config.credential_env, "")) if value)
            for secret in secrets:
                prompt = prompt.replace(secret, "[redacted]")
            features, _ = build_features(prompt=prompt,
                repo_path=str(root), role=role,
                entry_point=entry_point, brain=brain, prompt_egress=policy_config.prompt_egress,
                max_prompt_bytes=policy_config.max_prompt_bytes)
            features["prompt_bytes"] = prompt_bytes
        except (ValueError, TypeError, AttributeError, OSError, RecursionError):
            raise MetricsQueryError("invalid sandbox input or JEV configuration", "invalid_simulation") from None

        if not self._routing_tests.acquire(blocking=False):
            raise MetricsQueryError("routing simulation is busy", "metrics_busy")
        started = time.perf_counter()
        try:
            async with asyncio.timeout(policy_config.deadline_ms / 1000):
                result = await policy.decide(PolicyRequest(config.feature_schema_version, features,
                    policy_config.candidate_models, config.control_model, policy_config.version),
                    timeout_s=policy_config.deadline_ms / 1000)
        except TimeoutError:
            raise MetricsQueryError("JEV simulation timed out", "policy_timeout") from None
        except PolicyError as error:
            message = ("TypeSafe rejected the API key, or none is configured" if error.authentication
                       else "JEV simulation failed")
            raise MetricsQueryError(message, error.reason) from None
        except Exception:
            raise MetricsQueryError("JEV simulation failed", "policy_unavailable") from None
        finally:
            self._routing_tests.release()
        latency_ms = (time.perf_counter() - started) * 1000
        fallback = guard_result(result, policy_config)
        if fallback and fallback not in {"policy_low_confidence", "policy_version_drift"}:
            raise MetricsQueryError("JEV simulation returned an invalid decision", fallback)

        def safe_wire(value: object, depth: int = 0) -> object:
            if depth > 32:
                raise MetricsQueryError("JEV simulation returned an invalid decision", "policy_invalid_response")
            if isinstance(value, dict):
                return {safe_wire(name, depth + 1): "[redacted]" if re.search(
                        r"secret|password|passwd|credential|api.?key|authorization|(?:^|[_-])token(?:$|[_-])", name, re.I)
                        else safe_wire(item, depth + 1) for name, item in value.items()}
            if isinstance(value, list):
                return [safe_wire(item, depth + 1) for item in value]
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[redacted]")
                return redact_secrets(value)[0]
            return value

        data = {"selected_model": result.model, "confidence": result.confidence,
                "probabilities": dict(result.probabilities or {}), "latency_ms": latency_ms,
                "features": safe_wire(features), "outgoing_payload": safe_wire(result.outgoing_payload),
                "raw_response": safe_wire(result.raw_response), "fallback_reason": fallback,
                "effective_model": config.control_model if fallback or policy_config.mode == "shadow" else result.model,
                "mode": policy_config.mode, "prompt_egress": policy_config.prompt_egress,
                "model_version": result.model_version, "pinned_version": policy_config.version}
        from bobi.metrics.query import MAX_RESPONSE_BYTES

        if len(json.dumps(data, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES:
            raise MetricsQueryError("JEV simulation returned an oversized response", "policy_invalid_response")
        return data

    def metrics(self, name: str, view: str, args: dict) -> dict:
        import json

        from bobi.chat_history import safe_name
        from bobi.metrics.query import MetricsQueries, MetricsQueryError

        if not safe_name(name):
            raise UnknownTeam(name)
        root = self._resolve(name)
        if not root.is_relative_to(paths.agents_root().resolve()):
            raise UnknownTeam(name)
        if not self._metrics_reads.acquire(blocking=False):
            raise MetricsQueryError("metrics readers are busy", "metrics_busy", retry_after_ms=250)
        try:
            queries = MetricsQueries(root)
            data = {"summary": queries.summary, "turns": queries.turns, "sessions": queries.sessions,
                    "session": queries.session, "turn": queries.turn}[view](
                        {**args, "include_breakdown": True} if view == "turn" else args)
            if view in {"turn", "turns"}:
                from bobi.chat_history import read_transcript_detail

                transcripts = {}
                turns = data["turns"] if view == "turns" else [data["turn"]]
                transcript_deadline = time.monotonic() + 1.0
                for turn in turns:
                    session = turn if view == "turns" else data["session"]
                    provider_id = session.get("provider_session_id")
                    if not isinstance(provider_id, str) or not safe_name(provider_id):
                        provider_id = None
                    next_started = turn.get("next_started_at_us") if view == "turns" else data["next_started_at_us"]
                    key = (provider_id, session.get("brain"))
                    if key not in transcripts:
                        try:
                            group = [item for item in turns if item["session_id"] == turn["session_id"]]
                            ends = [item.get("ended_at_us") or item.get("next_started_at_us") for item in group]
                            before = max(ends) + 1 if all(value is not None for value in ends) else None
                            if any(item.get("status") == "running" for item in group):
                                before = None
                            transcripts[key] = read_transcript_detail(provider_id, limit=len(group) * 200 + 1,
                                brain=key[1], after_us=min(item["started_at_us"] for item in group), before_us=before
                            ) if provider_id and time.monotonic() < transcript_deadline else []
                        except (OSError, UnicodeError):
                            transcripts[key] = []
                    conversation = _turn_conversation(turn, transcripts[key], next_started, session=session)
                    turn["semantic_title"] = conversation["semantic_title"]
                    if view == "turns":
                        turn["conversation"] = {key: conversation[key] for key in ("status", "origin", "prompt_snippet", "response_snippet", "user_message", "semantic_title")}
                    else:
                        data["conversation"] = conversation
            if view == "summary":
                try:
                    health = json.loads((root / "state/metrics/collector.state.json").read_text())
                except (OSError, ValueError):
                    health = {}
                if not isinstance(health, dict):
                    health = {}
                data["collector"] = {key: health.get(key) for key in (
                    "status", "db_ready", "import_lag_ms", "last_success_at_us",
                    "reconciliation_errors", "uncovered_turns", "telemetry_events_dropped",
                )}
            from bobi.metrics.query import MAX_RESPONSE_BYTES

            if len(json.dumps(data, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES:
                raise MetricsQueryError("response exceeds 512 KiB; narrow the query", "query_too_large")
            return data
        finally:
            self._metrics_reads.release()

    def get_routing_config(self, name: str) -> dict:
        import json
        from bobi import paths
        from bobi.config import project_env
        from bobi.metrics.router import load_experiment
        from bobi.metrics.policies.typesafe import TypeSafePolicy
        from bobi.prompts.resolver import discover_roles
        from bobi.chat_history import safe_name
        from bobi.costs import model_prices, native_models, routable_models

        if not safe_name(name):
            raise UnknownTeam(name)
        root = self._resolve(name)
        if not root.is_relative_to(paths.agents_root().resolve()):
            raise UnknownTeam(name)
        env = project_env(root)
        env_file = paths.env_path(root)

        try:
            available_roles = [r.get("name", "") for r in discover_roles(project_path=root) if r.get("name")]
        except Exception:
            available_roles = []
        if not available_roles:
            available_roles = ["director", "engineer"]
        if "director" not in available_roles:
            available_roles.insert(0, "director")
        if "engineer" not in available_roles:
            available_roles.append("engineer")

        raw_val = env.get("BOBI_METRICS_EXPERIMENT_JSON", "").strip()
        is_commented = False
        if not raw_val and env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    if line.startswith("# BOBI_METRICS_EXPERIMENT_JSON="):
                        raw_val = line.split("=", 1)[1].strip()
                        is_commented = True
                        break
            except OSError:
                pass

        config_error = ""
        try:
            configured = load_experiment(env)
            if configured and configured[0].policy:
                TypeSafePolicy(configured[0].policy)
        except (ValueError, TypeError) as exc:
            configured = None
            config_error = str(exc)

        agent_brain, gateway = _agent_brain(root, env)

        def model_fields(candidates: list[str], control: str, policy_brain: str) -> dict:
            return {
                "agent_brain": agent_brain,
                "gateway": gateway,
                "config_warnings": _routing_problems(policy_brain, candidates, agent_brain, gateway),
                # The picker lists what the agent's own brain can run; priced_models
                # (gateway forms included) tells the editor which typed ids have a list price.
                "native_models": native_models(agent_brain),
                "priced_models": routable_models() + native_models("codex"),
                "unpriced_models": sorted({m for m in (*candidates, control) if m and model_prices(m) is None}),
            }

        if configured is not None:
            config, _ = configured
            policy = config.policy
            formatted_json = ""
            try:
                formatted_json = json.dumps(json.loads(raw_val or "{}"), indent=2)
            except Exception:
                pass
            return {
                "enabled": True,
                "raw_json": formatted_json,
                "experiment_id": config.experiment_id,
                "cohort": config.cohort or "",
                "control_model": config.control_model,
                "policy_name": policy.name if policy else "",
                "policy_version": policy.version if policy else "",
                "mode": policy.mode if policy else "enforce",
                "candidate_models": list(policy.candidate_models) if policy else [v.model for v in config.variants if v.model],
                "roles": list(policy.roles) if policy else [],
                "available_roles": available_roles,
                "entry_points": list(policy.entry_points) if policy else [],
                "min_confidence": policy.min_confidence if policy else 0.85,
                "deadline_ms": policy.deadline_ms if policy else 3000,
                "credential_env": policy.credential_env if policy else "TYPESAFE_API_KEY",
                "endpoint": json.loads(policy.options_json).get("endpoint", "https://api.typesafe.ai/v1/systemone") if policy else "https://api.typesafe.ai/v1/systemone",
                "brain": policy.brain if policy else "auto",
                "prompt_egress": policy.prompt_egress if policy else "none",
                "instructions": json.loads(policy.options_json).get("instructions", "") if policy else "",
                "applies_on": "agent_restart",
                "restart_required": True,
                "config_path": str(env_file),
                **model_fields(list(policy.candidate_models) if policy else [v.model for v in config.variants if v.model],
                               config.control_model or "", policy.brain if policy else "auto"),
            }

        formatted_json = ""
        parsed_data = {}
        if raw_val:
            try:
                parsed_data = json.loads(raw_val)
                formatted_json = json.dumps(parsed_data, indent=2)
            except Exception:
                config_error = config_error or "BOBI_METRICS_EXPERIMENT_JSON is not valid JSON"

        from bobi.metrics.router import ExperimentConfig

        if not isinstance(parsed_data, dict):
            parsed_data = {}
        try:
            experiment = ExperimentConfig.from_mapping(parsed_data)
            if experiment.policy:
                TypeSafePolicy(experiment.policy)
            policy_dict = experiment.policy.public_config() if experiment.policy else {}
            if experiment.policy:
                policy_dict["options"] = json.loads(experiment.policy.options_json)
        except (ValueError, TypeError) as exc:
            if parsed_data:
                config_error = config_error or str(exc)
            formatted_json = ""
            parsed_data = {}
            policy_dict = {}
        # Nothing saved: no invented models. The editor asks for candidates.
        control = parsed_data.get("control_model") or ""
        mode = policy_dict.get("mode") or "shadow"
        candidates = policy_dict.get("candidate_models") or ([control] if control else [])
        scope = policy_dict.get("scope") or {}
        roles = scope.get("roles") or ["engineer", "director"]
        entry_points = scope.get("entry_points") or ["subagent_persistent", "subagent_phase", "workflow_start"]
        options = policy_dict.get("options")
        endpoint = options.get("endpoint") if isinstance(options, dict) else None

        return {
            "enabled": False,
            "raw_json": formatted_json,
            "experiment_id": parsed_data.get("experiment_id", ""),
            "cohort": parsed_data.get("cohort", "production-opt-in"),
            "control_model": control,
            "policy_name": policy_dict.get("name", "typesafe-jev"),
            "policy_version": policy_dict.get("version", "jev-1.13.0"),
            "mode": "off",
            "configured_mode": mode,
            "candidate_models": list(candidates),
            "roles": list(roles),
            "available_roles": available_roles,
            "entry_points": list(entry_points),
            "min_confidence": policy_dict.get("min_confidence", 0.85),
            "deadline_ms": policy_dict.get("deadline_ms", 3000),
            "credential_env": policy_dict.get("credential_env", "TYPESAFE_API_KEY"),
            "endpoint": endpoint if isinstance(endpoint, str) else "https://api.typesafe.ai/v1/systemone",
            "brain": policy_dict.get("brain", "auto"),
            # A new policy recommends redacted task text; a saved one keeps its choice.
            "prompt_egress": (policy_dict.get("egress") or {}).get("prompt", "redacted"),
            "instructions": options.get("instructions", "") if isinstance(options, dict) else "",
            "applies_on": "agent_restart",
            "restart_required": True,
            "config_path": str(env_file),
            "config_error": config_error,
            **model_fields(list(candidates), control, policy_dict.get("brain", "auto")),
        }

    def update_routing_config(self, name: str, payload: dict) -> dict:
        import json
        import os
        import secrets

        from bobi.config import _DOTENV_LOADED, project_env
        from bobi.fsutil import atomic_write_text, file_lock
        from bobi.metrics.query import MetricsQueryError
        from bobi.metrics.router import ASSIGNMENT_SECRET_ENV, EXPERIMENT_CONFIG_ENV, ExperimentConfig
        from bobi.metrics.policies.typesafe import TypeSafePolicy
        from bobi.chat_history import safe_name
        from bobi.costs import model_identity

        if not safe_name(name):
            raise UnknownTeam(name)
        root = self._resolve(name)
        if not root.is_relative_to(paths.agents_root().resolve()):
            raise UnknownTeam(name)
        env_file = paths.env_path(root)
        allowed = {"enabled", "mode", "control_model", "candidate_models", "roles", "instructions",
                   "min_confidence", "experiment_id", "brain", "prompt_egress"}
        try:
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            if set(payload) - allowed:
                raise ValueError("unsupported fields: " + ", ".join(sorted(set(payload) - allowed)))
            if "enabled" in payload and type(payload["enabled"]) is not bool:
                raise ValueError("enabled must be true or false")
            if "mode" in payload and payload["mode"] not in ("off", "shadow", "enforce"):
                raise ValueError("mode must be off, shadow or enforce")
            if payload.get("mode") == "off" and payload.get("enabled") is True:
                raise ValueError("mode off conflicts with enabled")
            for field in ("control_model", "experiment_id", "instructions"):
                if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()
                                         or len(payload[field].encode("utf-8")) > 32768):
                    raise ValueError(f"{field} must be non-empty text of at most 32768 bytes")
            for field in ("candidate_models", "roles"):
                if field in payload:
                    values = payload[field]
                    if (not isinstance(values, list) or not values or len(values) > 100
                            or any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values)
                            or len(set(value.strip() for value in values)) != len(values)):
                        raise ValueError(f"{field} must list 1 to 100 unique, non-empty names")
            if "brain" in payload and payload["brain"] not in ("auto", "claude", "codex"):
                raise ValueError("brain must be auto, claude or codex")
            if "prompt_egress" in payload and payload["prompt_egress"] not in ("redacted", "none"):
                raise ValueError("prompt_egress must be redacted or none")
            if "candidate_models" in payload:
                seen: dict[str, str] = {}
                for model in payload["candidate_models"]:
                    other = seen.setdefault(model_identity(model), model)
                    if other != model:
                        # The policy would split its probability across two names for one model.
                        raise ValueError(f"candidate_models name one model twice: {other} and {model}")
            if "min_confidence" in payload:
                import math

                value = payload["min_confidence"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("min_confidence must be between 0 and 1")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise MetricsQueryError(f"invalid JEV configuration: {exc}", "invalid_jev_config") from None
        if EXPERIMENT_CONFIG_ENV in os.environ and _DOTENV_LOADED.get(EXPERIMENT_CONFIG_ENV) != os.environ[EXPERIMENT_CONFIG_ENV]:
            raise MetricsQueryError("JEV configuration is controlled by the process environment", "config_environment_override")
        if (payload.get("enabled") is False or payload.get("mode") == "off") and (
                ASSIGNMENT_SECRET_ENV in os.environ and _DOTENV_LOADED.get(ASSIGNMENT_SECRET_ENV) != os.environ[ASSIGNMENT_SECRET_ENV]):
            raise MetricsQueryError("JEV configuration is controlled by the process environment", "config_environment_override")
        try:
            with file_lock(env_file):
                text = env_file.read_text() if env_file.exists() else ""
                lines = text.splitlines()
                current = self.get_routing_config(name)
                enabled = payload.get("enabled", payload.get("mode") != "off")
                raw = current.get("raw_json")
                previous = json.loads(raw) if raw else {}
                if not enabled:
                    # Switching the mode to off keeps the policy as a commented line so it
                    # can be re-enabled; DELETE (enabled: false) removes it.
                    config_json = json.dumps(previous, ensure_ascii=True) if previous and payload.get("mode") == "off" else ""
                else:
                    fields = set(payload) - {"enabled"}
                    if not fields and previous:
                        config_dict = previous
                    else:
                        config_dict = dict(previous) if previous else {
                            "experiment_id": current["experiment_id"] or "jev-routing-v1",
                            "mode": current.get("configured_mode", current["mode"]),
                            "control_model": current["control_model"],
                            "candidate_models": current["candidate_models"],
                            "roles": current["roles"],
                            "instructions": current["instructions"] or "Choose the least costly candidate that can reliably complete the task.",
                            "min_confidence": current["min_confidence"],
                            "brain": current["brain"],
                            "prompt_egress": current["prompt_egress"],
                        }
                        if config_dict.get("mode") == "off":
                            config_dict["mode"] = "shadow"
                        if "policy" in config_dict:
                            policy = config_dict["policy"]
                            previous_control = config_dict["control_model"]
                            for field in ("experiment_id", "control_model"):
                                if field in payload:
                                    config_dict[field] = payload[field]
                            for variant in config_dict["variants"]:
                                if variant.get("model") == previous_control:
                                    variant["model"] = config_dict["control_model"]
                            for field in ("mode", "candidate_models", "min_confidence", "brain"):
                                if field in payload:
                                    policy[field] = payload[field]
                            if "prompt_egress" in payload:
                                policy["egress"]["prompt"] = payload["prompt_egress"]
                            if "roles" in payload:
                                policy["scope"]["roles"] = payload["roles"]
                            if "instructions" in payload:
                                policy["options"]["instructions"] = payload["instructions"]
                            criteria = policy["options"].get("criteria", {})
                            policy["options"]["criteria"] = {model: criteria.get(model, f"Choose {model} when the routing instructions recommend it.")
                                                             for model in policy["candidate_models"]}
                        else:
                            config_dict.update({field: value for field, value in payload.items() if field != "enabled"})
                            if "criteria" in config_dict:
                                criteria = config_dict["criteria"]
                                config_dict["criteria"] = {model: criteria.get(model, f"Choose {model} when the routing instructions recommend it.")
                                                          for model in config_dict["candidate_models"]}
                    experiment = ExperimentConfig.from_mapping(config_dict)
                    TypeSafePolicy(experiment.policy)
                    if experiment.control_model not in experiment.policy.candidate_models:
                        raise ValueError("control_model must be one of candidate_models")
                    problems = _routing_problems(experiment.policy.brain, list(experiment.policy.candidate_models),
                                                 current["agent_brain"], current["gateway"])
                    if problems:
                        raise ValueError(problems[0] + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""))
                    if set(experiment.policy.roles) - set(current["available_roles"]):
                        raise ValueError("unknown roles: " + ", ".join(sorted(set(experiment.policy.roles) - set(current["available_roles"]))))
                    config_json = json.dumps(config_dict, ensure_ascii=True, allow_nan=False)
                replacement = [f"{EXPERIMENT_CONFIG_ENV}={config_json}"] if enabled else (
                    [f"# {EXPERIMENT_CONFIG_ENV}={config_json}", f"{EXPERIMENT_CONFIG_ENV}="] if config_json else [f"{EXPERIMENT_CONFIG_ENV}="])
                new_lines = []
                replaced = False
                for line in lines:
                    if re.match(r"^\s*(?:#\s*)?BOBI_METRICS_EXPERIMENT_JSON\s*=", line):
                        if not replaced:
                            new_lines.extend(replacement)
                            replaced = True
                    else:
                        new_lines.append(line)
                if not replaced:
                    new_lines.extend(replacement)
                if not enabled:
                    new_lines = [line for line in new_lines if not re.match(r"^\s*BOBI_METRICS_ASSIGNMENT_SECRET\s*=", line)]
                    new_lines.append(f"{ASSIGNMENT_SECRET_ENV}=")
                if enabled and not project_env(root).get(ASSIGNMENT_SECRET_ENV):
                    new_lines = [line for line in new_lines if not re.match(r"^\s*BOBI_METRICS_ASSIGNMENT_SECRET\s*=", line)]
                    new_lines.append(f"{ASSIGNMENT_SECRET_ENV}={secrets.token_urlsafe(32)}")
                atomic_write_text(env_file, "\n".join(new_lines) + "\n", mode=0o600, fsync=True)
                return self.get_routing_config(name)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise MetricsQueryError(f"invalid JEV configuration: {exc}", "invalid_jev_config") from None
        except AttributeError:
            raise MetricsQueryError("invalid JEV configuration: saved policy has an unexpected shape", "invalid_jev_config") from None
        except OSError:
            raise MetricsQueryError("JEV configuration could not be persisted", "config_write_failed") from None

    def dashboard(self) -> dict:
        """Every agent slot on this machine: installed (with run state)
        first, then design-only sources."""
        installed = paths.list_agents()
        cards = [agent_card(name) for name in installed]

        agents_root = paths.agents_root()
        if agents_root.is_dir():
            for d in sorted(agents_root.iterdir()):
                if (d.is_dir() and d.name not in installed
                        and (d / "src" / "agent.yaml").is_file()):
                    cards.append(design_card(d.name))
        return {"agents": cards, "home": str(paths.home_dir())}

    def team_status(self, name: str) -> dict:
        self._resolve(name)
        return agent_card(name)

    def start_team(self, name: str) -> dict:
        from bobi import service

        root = self._resolve(name)
        try:
            with self._spawn_lock:
                result = service.spawn_team(root)
        except service.AlreadyRunning as e:
            raise TeamAlreadyRunning(e.pid) from e
        except service.PreflightFailed as e:
            raise TeamPreflightFailed(e.validation.format()) from e
        except service.ServiceError as e:
            raise TeamLifecycleError(str(e)) from e
        return {"ok": True, "pid": result.startup.pid}

    def stop_team(self, name: str) -> dict:
        from bobi import service

        root = self._resolve(name)
        result = service.stop_team(root)
        return {
            "ok": result.stopped or result.killed or result.stale
                  or result.pid == 0,
            "stopped": result.stopped,
            "pid": result.pid,
            "still_running": result.still_running,
        }

    def restart_team(self, name: str) -> dict:
        from bobi import service

        root = self._resolve(name)
        stop = service.stop_team(root)
        if stop.still_running:
            raise TeamDidNotStop(stop.pid)
        # One spawn path: restart is stop + start, so a preflight failure
        # carries the same structured report either way.
        return self.start_team(name)

    def subagents(self, name: str) -> list[dict]:
        from bobi import service

        root = self._resolve(name)
        mgr = service.manager_session_name(root)
        entries = service.list_agents(root)
        return [serialize_subagent(e, manager_name=mgr)
                for e in ordered_subagents(entries, manager_name=mgr)]

    def messages(self, name: str, session: str) -> list[dict]:
        from bobi.sdk import load_session_brain, load_session_id

        root = self._resolve(name)
        # The durable source of truth is the session transcript; the web-UI
        # chat log is the fallback when no transcript resolves yet. The recorded
        # brain picks the transcript format (Codex rollout vs Claude JSONL).
        # Both are explicit-path reads.
        messages = read_transcript_messages(
            load_session_id(session, root=root),
            brain=load_session_brain(session, root=root),
        )
        if not messages:
            messages = read_chat(root, session)
        return messages

    def transcript(self, name: str, session: str) -> dict:
        """The debugging view of the same transcript ``messages`` reads."""
        from bobi.chat_history import read_transcript_detail
        from bobi.sdk import load_session_brain, load_session_id

        root = self._resolve(name)
        entries = read_transcript_detail(
            load_session_id(session, root=root),
            brain=load_session_brain(session, root=root),
        )
        return {"session": session, "entries": entries,
                "usage": session_usage(root, session)}

    def run_details(self, name: str, run_id: str) -> dict:
        """The Details slab for a run with no transcript to open."""
        from bobi.webapp.details import UnknownRun as DetailsUnknownRun
        from bobi.webapp.details import build_details

        root = self._resolve(name)
        try:
            return build_details(root, run_id)
        except DetailsUnknownRun:
            raise UnknownRun(run_id) from None

    def system_logs(self, name: str, lines: int = 200) -> dict:
        """Recent daemon system log lines (from manager.log) and health error detection."""
        root = self._resolve(name)
        log_file = paths.manager_log_path(root)
        if not log_file.exists():
            return {"ok": True, "logs": [], "total_lines": 0, "path": str(log_file)}
        try:
            content = log_file.read_text(errors="replace").splitlines()
            tail = content[-lines:] if lines > 0 else content
            recent_errors = [
                line for line in tail[-50:]
                if "[ERROR]" in line or " 521" in line or "Error 521" in line
            ]
            return {
                "ok": True,
                "logs": tail,
                "total_lines": len(content),
                "recent_error": recent_errors[-1] if recent_errors else None,
                "path": str(log_file),
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "logs": []}

    def _prune_jobs(self) -> None:
        # Caller holds _chat_lock.
        if len(self._chat_jobs) <= 500:
            return
        for mid in [m for m, j in self._chat_jobs.items()
                    if j["status"] != "pending"][:250]:
            self._chat_jobs.pop(mid, None)

    def chat_submit(self, name: str, session: str, text: str) -> str:
        root = self._resolve(name)
        message_id = uuid.uuid4().hex
        with self._chat_lock:
            self._prune_jobs()
            self._chat_jobs[message_id] = {"team": name, "status": "pending"}

        def work() -> None:
            from bobi import service

            try:
                # An empty session means the team manager. The route documents
                # `subagent` as optional and the hosted runtime has always read
                # it that way (supervisor/admin.py), so both runtimes resolve
                # it identically rather than one answering `unknown agent ''`.
                target = (session or "").strip() \
                    or service.manager_session_name(root)
                service.ask(root, target, text,
                            timeout=DEFAULT_CHAT_TIMEOUT)
                outcome = {"team": name, "status": "done"}
            except Exception as e:  # noqa: BLE001 - job must resolve
                outcome = {"team": name, "status": "error", "error": str(e)}
            with self._chat_lock:
                self._chat_jobs[message_id] = outcome

        threading.Thread(target=work, daemon=True,
                         name=f"chat-{message_id[:8]}").start()
        return message_id

    def chat_job(self, name: str, message_id: str) -> dict | None:
        with self._chat_lock:
            job = self._chat_jobs.get(message_id)
        # A job is only visible under the team it was submitted for.
        if job is None or job.get("team") != name:
            return None
        return {k: v for k, v in job.items() if k != "team"}

    # --- observability (read-only, #733) --------------------------------

    def spend_summary(self, name: str) -> dict:
        from bobi.costs import rollup_costs
        from bobi.webapp.savings import script_cache_savings

        root = self._resolve(name)
        # sessions_path (not sessions_dir): a read endpoint must not mkdir.
        payload = rollup_costs(paths.sessions_path(root)).to_dict()
        # Counterfactual dollars, kept in their own block so nothing can
        # accidentally add them to a bill.
        payload["script_cache"] = script_cache_savings(root)
        return payload

    def overview(self, name: str) -> dict:
        from bobi.webapp.overview import build_overview

        return build_overview(self._resolve(name))

    def fleet_spend(self) -> dict:
        """Roll up spend across every installed team. Offline: reads each
        team's session state files directly, one rollup per team."""
        from bobi.costs import rollup_costs

        total = 0.0
        estimated = 0.0
        counted = 0
        teams: list[dict] = []
        for team in paths.list_agents():
            root = paths.agent_run_root(team)
            summary = rollup_costs(paths.sessions_path(root))
            # Sum the rounded per-team totals so the dashboard header always
            # equals the sum of the visible tiles (no sub-cent drift where the
            # header shows spend no tile accounts for).
            team_total = round(summary.total_cost_usd, 4)
            team_estimated = round(summary.estimated_cost_usd, 4)
            teams.append({
                "name": team,
                "total_cost_usd": team_total,
                "estimated_cost_usd": team_estimated,
                "sessions_counted": summary.sessions_counted,
            })
            total += team_total
            estimated += team_estimated
            counted += summary.sessions_counted
        teams.sort(key=lambda t: (t["total_cost_usd"]
                                  + t["estimated_cost_usd"]), reverse=True)
        return {
            "total_cost_usd": round(total, 4),
            "estimated_cost_usd": round(estimated, 4),
            "sessions_counted": counted,
            "teams": teams,
        }

    def health_summary(self, name: str) -> dict:
        """Manager liveness + session statuses from this machine's files -
        the same sources the dashboard card and the roster read (manager
        pidfile, session registry) - plus the manager's own health probe,
        which is what makes ``state`` a tri-state rather than pid presence
        rephrased. A local team shares this host, so ``reachability`` is
        "live" by construction; there is no supervisor here, so the restart
        fields are null and the lifecycle trail is empty - the hosted runtime
        fills those from its sidecar."""
        from bobi import service
        from bobi.webapp import health as health_state

        root = self._resolve(name)
        status = service.team_status(root)
        mgr_name = service.manager_session_name(root)
        entries = ordered_subagents(status.active_agents,
                                    manager_name=mgr_name)
        running = status.manager_running
        mgr_entry = next((e for e in entries if e.name == mgr_name), None)
        if not running:
            mgr_status = "stopped"
        elif mgr_entry is not None and mgr_entry.status:
            mgr_status = mgr_entry.status
        else:
            # Manager pid alive but no registered manager session yet: the
            # boot window. Same fail-open verdict the hosted sidecar reports.
            mgr_status = "starting"

        # The strip's SINCE/EXIT/WAS UP come from the manager's last TERMINAL
        # record, which by definition is not in the active list - so the
        # stopped path pays one extra registry read, and only it does.
        strip_entry = mgr_entry
        if not running:
            strip_entry = self._last_manager_record(root, mgr_name)

        # "Last activity" is the agent's last sign of life, not the manager's
        # alone: a subagent still working is the newest thing that happened.
        last_activity = max((e.last_activity or 0.0 for e in entries),
                            default=0.0)
        state = health_state.build_state(
            running=running,
            pid=status.manager_pid,
            root=root,
            manager_entry=strip_entry,
            live_runs=sum(1 for e in entries if e.name != mgr_name),
            last_activity=last_activity,
        )
        return {
            "reachability": "live",
            "last_heartbeat_at": None,
            "state": state["state"],
            "detail": state["detail"],
            "segments": state["segments"],
            "manager": {
                "status": mgr_status,
                "pid": status.manager_pid,
                "running": running,
                # A wedged manager is not healthy, whatever its pid says.
                # This is the #887 defect: process-alive was read as healthy.
                "healthy": running and state["probe_ok"] is not False,
                "restart_count": None,
                "last_restart_reason": None,
                "last_restart_at": None,
                "idle_seconds": state["idle_seconds"],
            },
            "sessions": [{"name": e.name, "role": e.role, "status": e.status}
                         for e in entries],
            "lifecycle": [],
        }

    @staticmethod
    def _last_manager_record(root: Path, mgr_name: str):
        """The manager's registry entry including terminal ones, or None.

        Read with dead-pid reaping so a manager killed without reporting a
        terminal status reads as crashed here too, never as still running.
        """
        from bobi.sdk import SessionRegistry

        return next((e for e in SessionRegistry(root).list_all(reap_dead=True)
                     if e.name == mgr_name), None)

    def session_log(self, name: str) -> dict:
        """The whole registry, terminal sessions included. ``reap_dead``
        runs the same dead-pid crash marking as the roster read, so a
        session whose process died reads ``crashed`` here, never
        ``running``. Local responses are never capped - the history is on
        this disk."""
        from bobi import service
        from bobi.sdk import SessionRegistry

        root = self._resolve(name)
        mgr = service.manager_session_name(root)
        entries = ordered_session_log(
            SessionRegistry(root).list_all(reap_dead=True))
        return {
            "sessions": [serialize_session(e, manager_name=mgr)
                         for e in entries],
            "counts": session_outcome_counts(entries),
            "truncated": False,
        }

    # The three writes delegate to bobi.webapp.run_actions, which the
    # supervisor's admin commands call too - one implementation, both
    # runtimes. Only the error vocabulary is mapped here: the shared module
    # raises its own exceptions so it can stay free of this one.
    def resume_run(self, name: str, run_id: str, *, verdict: str = "",
                   reply: str = "") -> dict:
        return self._run_action("resume_run", name, run_id,
                                verdict=verdict, reply=reply)

    def remind_run(self, name: str, run_id: str) -> dict:
        return self._run_action("remind_run", name, run_id)

    def close_run(self, name: str, run_id: str) -> dict:
        return self._run_action("close_run", name, run_id)

    def _run_action(self, action: str, name: str, run_id: str, **kw) -> dict:
        from bobi.webapp import run_actions

        root = self._resolve(name)
        try:
            return getattr(run_actions, action)(root, run_id, **kw)
        except run_actions.UnknownRun:
            raise UnknownRun(run_id) from None
        except (run_actions.RunNotWaiting, run_actions.ActionFailed) as e:
            raise TeamLifecycleError(str(e)) from None

    def runs(self, name: str, *, status: str = "", kind: str = "", query: str = "",
             offset: int = 0, limit: int | None = None) -> dict:
        """Fold this machine's three run stores for one team. Every read takes
        the resolved root explicitly - this process serves every team and
        binds none of them."""
        from bobi import service
        from bobi.webapp.runs import DEFAULT_LIMIT, build_runs

        root = self._resolve(name)
        return build_runs(
            root,
            manager_name=service.manager_session_name(root),
            status=status or "",
            kind=kind or "",
            query=query or "",
            offset=max(0, offset),
            limit=limit if limit and limit > 0 else DEFAULT_LIMIT,
        )
