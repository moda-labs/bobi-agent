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

import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from bobi import paths
from bobi.chat_history import read_chat, read_transcript_messages

DEFAULT_CHAT_TIMEOUT = 300


# --- Errors -----------------------------------------------------------------

class TeamRuntimeError(Exception):
    """Base for runtime-surface errors the handlers translate to HTTP."""


class UnknownTeam(TeamRuntimeError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown agent '{name}'")


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

    Widening this ABC: an out-of-tree subclass lives in the private
    bobi-deploy repo (``EventBusRuntime``), whose CI tracks this repo's
    ``dev`` channel (auto-advanced to every green main push, #740). Adding
    an ``@abstractmethod`` here therefore breaks that repo's CI the moment
    this repo merges - Python rejects instantiating the subclass until it
    implements the method. Sequencing rule: land the private subclass
    implementation FIRST, then the abstract method here (an extra method on
    a subclass is harmless; see the #733 system-health PR pair). Keep new
    methods read-only-safe and document the wire shape in the docstring, as
    below - both runtimes must emit it identically, it is rendered once.
    """

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
             "by_provider", "by_model", "by_session", "by_role"}
        """

    @abstractmethod
    def fleet_spend(self) -> dict:
        """Fleet-wide spend for the dashboard: a total plus per-team totals::

            {"total_cost_usd", "sessions_counted",
             "teams": [{"name", "total_cost_usd", "sessions_counted"}, ...]}
        """

    @abstractmethod
    def health_summary(self, name: str) -> dict:
        """One team's system health: manager liveness, session statuses, and
        (where a supervisor records one) the recent lifecycle trail.

        Shape::

            {"reachability": "live" | "stale" | "unreachable",
             "last_heartbeat_at": str | None,
             "manager": {"status", "pid", "running", "healthy",
                         "restart_count", "last_restart_reason",
                         "last_restart_at", "idle_seconds"},
             "sessions": [{"name", "role", "status"}, ...],
             "lifecycle": [{"event", "received_at", "at", ...}, ...]}

        ``lifecycle`` is newest first. Fields a runtime cannot know are
        null/empty rather than omitted (a local team has no heartbeats and no
        supervisor trail), so render code branches on value, never on key
        presence.
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


class LocalRuntime(TeamRuntime):
    """Today's behavior: this machine's agents tree, explicit-root service
    calls, chat delivered by a background thread per submit."""

    def __init__(self) -> None:
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
                service.ask(root, session, text,
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

        root = self._resolve(name)
        # sessions_path (not sessions_dir): a read endpoint must not mkdir.
        return rollup_costs(paths.sessions_path(root)).to_dict()

    def fleet_spend(self) -> dict:
        """Roll up spend across every installed team. Offline: reads each
        team's session state files directly, one rollup per team."""
        from bobi.costs import rollup_costs

        total = 0.0
        counted = 0
        teams: list[dict] = []
        for team in paths.list_agents():
            root = paths.agent_run_root(team)
            summary = rollup_costs(paths.sessions_path(root))
            # Sum the rounded per-team totals so the dashboard header always
            # equals the sum of the visible tiles (no sub-cent drift where the
            # header shows spend no tile accounts for).
            team_total = round(summary.total_cost_usd, 4)
            teams.append({
                "name": team,
                "total_cost_usd": team_total,
                "sessions_counted": summary.sessions_counted,
            })
            total += team_total
            counted += summary.sessions_counted
        teams.sort(key=lambda t: t["total_cost_usd"], reverse=True)
        return {
            "total_cost_usd": round(total, 4),
            "sessions_counted": counted,
            "teams": teams,
        }

    def health_summary(self, name: str) -> dict:
        """Manager liveness + session statuses from this machine's files -
        the same sources the dashboard card and the roster read (manager
        pidfile, session registry). A local team shares this host, so
        ``reachability`` is "live" by construction; there is no supervisor
        here, so the restart fields are null and the lifecycle trail is
        empty - the hosted runtime fills those from its sidecar."""
        from bobi import service

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
        return {
            "reachability": "live",
            "last_heartbeat_at": None,
            "manager": {
                "status": mgr_status,
                "pid": status.manager_pid,
                "running": running,
                "healthy": running,
                "restart_count": None,
                "last_restart_reason": None,
                "last_restart_at": None,
                "idle_seconds": None,
            },
            "sessions": [{"name": e.name, "role": e.role, "status": e.status}
                         for e in entries],
            "lifecycle": [],
        }

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
