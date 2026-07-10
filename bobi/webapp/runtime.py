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
