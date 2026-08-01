"""Inbound admin command path (issue #9).

The sidecar's OTHER half. Phase A (#5/#6) publishes telemetry OUTBOUND; this
listens for operator admin commands on the deployment's per-instance admin topic
and executes them against the :class:`~.supervision.Supervisor`.

Independent by construction: its own :class:`EventServerClient` (own WS, own
cursor) inside the SAME instance bubble the telemetry layer joined. Bubble
namespacing makes the topic fail-closed - the server publishes a command INTO
the target deployment's bubble, and only that deployment (same bubble, this
exact topic string) can receive it. It runs in the SUPERVISOR process, not the
manager, so admin commands keep working when the manager is wedged - the whole
reason the sidecar exists.

Command semantics:
- ``restart`` / ``stop`` / ``start`` are queued to the supervisor's main loop
  (process management stays single-threaded) and acknowledged immediately as
  ``done`` with ``{"accepted": true}``. The lifecycle EFFECT (restart count,
  manager status, stopped/started) surfaces on the tier-1 heartbeat read model,
  which is where an operator confirms it landed.
- ``status`` echoes the latest heartbeat snapshot.
- ``transcript`` / ``roster`` / ``spend`` / ``session_log`` are tier-3
  on-demand reads answered INLINE from the local ``$BOBI_HOME`` via the same
  public ``bobi`` surfaces the local web UI uses (``read_transcript_messages``
  / ``list_agents`` / ``rollup_costs`` / ``SessionRegistry.list_all``); the
  sidecar shares the manager's host, state dir, and Claude config, so these
  are plain filesystem reads. Result: ``{"messages": [...]}`` /
  ``{"subagents": [...]}`` / ``{"spend": {...}}`` /
  ``{"sessions": [...], "counts": {...}, "truncated": bool}``.
- ``chat`` (Phase C data plane) is the ONLY async command: a real turn can take
  minutes, and the dispatch worker is single-threaded and strictly ordered (a
  ``restart`` must stay responsive), so ``chat`` runs ``service.ask`` on a
  DETACHED thread and that thread owns the single ``command_result`` publish
  (``done`` once the turn's reply lands in the transcript, or ``error``). The
  reply reaches the operator via a follow-up ``transcript`` read, mirroring the
  local UI's submit-then-poll.

Every method is fail-open: a broker outage or a malformed command must never
take the supervisor down.
"""

from __future__ import annotations

import logging
import threading
from queue import Queue, SimpleQueue

from .bus import default_publish as _default_publish

log = logging.getLogger(__name__)

# These MUST match the server's fleet.ts (adminTopic / COMMAND_RESULT_TOPIC)
# byte-for-byte - the topic string is the producer/consumer contract.
COMMAND_RESULT_TOPIC = "fleet/command_result"
ADMIN_COMMANDS = frozenset({"restart", "stop", "start", "status",
                            "chat", "transcript", "roster", "spend",
                            "session_log"})

# The session log rides one bus message + one KV command record, so cap the
# row list (newest first - the cap keeps the recent end). counts is computed
# before the cap and `truncated` flags it, so totals stay honest. 100 rows
# is double what the shared frontend renders.
MAX_SESSION_LOG_ROWS = 100

# Sentinel that unblocks the worker loop on shutdown.
_STOP = object()


def admin_topic(fleet: str, instance: str) -> str:
    return f"fleet/admin/{fleet}/{instance}"


class AdminListener:
    """Subscribe to the deployment's admin topic and run operator commands.

    The ``register_fn`` / ``client_factory`` / ``publish_fn`` seams are injected
    by the unit tests so dispatch is exercised without a real broker; production
    uses the public ``bobi.events`` defaults.
    """

    def __init__(self, *, supervisor, telemetry, project_root=None,
                 identity: dict | None = None, event_server_url: str | None = None,
                 register_fn=None, client_factory=None,
                 publish_fn=_default_publish):
        self.supervisor = supervisor
        self.telemetry = telemetry
        self.project_root = project_root
        self.identity = identity or telemetry.identity
        self.fleet = self.identity.get("fleet")
        self.instance = self.identity.get("instance")
        self.topic = admin_topic(self.fleet, self.instance)
        self._event_server_url = event_server_url
        self._register_fn = register_fn
        self._client_factory = client_factory
        self._publish_fn = publish_fn
        self._client = None
        # Commands are executed off the WS callback thread: a slow result
        # publish (exactly the degraded conditions the sidecar exists for) must
        # not stall the socket's ping/pong or the next command. One worker keeps
        # commands strictly ordered.
        self._work: Queue = Queue()
        self._worker: threading.Thread | None = None

    # --- startup ----------------------------------------------------------

    def _resolve_url(self) -> str:
        if self._event_server_url:
            return self._event_server_url
        from bobi.config import Config
        return (Config.load(self.project_root).event_server_url
                or "http://localhost:8080")

    def start(self) -> None:
        """Register the admin subscription and open the listener. Fail-open: on
        any error the supervisor runs on without a command channel and retries
        are the operator's to trigger (re-issue once the broker recovers)."""
        try:
            url = self._resolve_url()
            deployment_id, api_key = self._register(url)
            self._client = self._make_client(url, deployment_id, api_key)
            self._worker = threading.Thread(target=self._worker_loop,
                                            name="admin-dispatch", daemon=True)
            self._worker.start()
            self._client.start()
            log.info("supervisor: admin listener subscribed on %s", self.topic)
        except Exception:
            log.warning("supervisor: admin listener failed to start - operator "
                        "commands unavailable until it recovers", exc_info=True)

    def _register(self, url: str):
        if self._register_fn is not None:
            return self._register_fn(url, self.topic)
        # JOIN the instance bubble (idempotent; telemetry already converged it)
        # and register a SEPARATE deployment for the admin subscription so the
        # listener has its own WS/cursor, independent of the manager's.
        from bobi.events.server import ensure_bubble, register
        bubble = ensure_bubble(url, self.project_root)
        return register(url, f"{self.instance}-admin", [self.topic],
                        bubble_id=bubble.get("bubble_id", ""),
                        bubble_key=bubble.get("bubble_key", ""))

    def _make_client(self, url: str, deployment_id: str, api_key: str):
        if self._client_factory is not None:
            return self._client_factory(url, deployment_id, api_key, self._on_event)
        from bobi import paths
        from bobi.events.client import EventServerClient
        # A DISTINCT cursor from the manager's shared default (state/cursor.json),
        # and a private queue we never drain (dispatch happens in on_event) so
        # admin events never touch the process-global event_queue.
        cursor = paths.state_path(self.project_root) / "admin-cursor.json"
        return EventServerClient(url, deployment_id, api_key,
                                 on_event=self._on_event,
                                 cursor_path=cursor, queue=SimpleQueue())

    # --- dispatch ---------------------------------------------------------

    def _on_event(self, event: dict) -> None:
        # Runs on the client's WS callback thread: hand off and return fast so a
        # slow command never blocks the socket.
        self._work.put(event or {})

    def _worker_loop(self) -> None:
        while True:
            item = self._work.get()
            if item is _STOP:
                return
            self._safe_dispatch(item)

    def _safe_dispatch(self, event: dict) -> None:
        try:
            self._dispatch(event)
        except Exception:
            log.exception("supervisor: admin command dispatch failed")

    def _dispatch(self, event: dict) -> None:
        payload = event.get("payload") or {}
        command_id = payload.get("command_id")
        command = payload.get("command")
        # Defense in depth: the topic is already operator-only via bubble
        # namespacing, but a malformed or unknown command is dropped, never
        # guessed. No reply - an unrecognized command has no command to ack.
        if not command_id or command not in ADMIN_COMMANDS:
            log.warning("supervisor: ignoring malformed admin command %r", payload)
            return
        log.info("supervisor: admin command %s (id=%s)", command, command_id)
        # The server is command-agnostic and does not validate arg shape, so a
        # malformed `args` (string/array/null) must not reach `.get` unguarded -
        # a raw AttributeError here would be swallowed by _safe_dispatch and
        # leave the command pending forever. Coerce to a dict once, for all verbs.
        raw_args = payload.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        # Chat is the one async command: hand off to a detached worker that owns
        # the result publish, so the (potentially minutes-long) turn never blocks
        # this ordered dispatch worker or a queued restart.
        if command == "chat":
            self._dispatch_chat_async(command_id, args)
            return
        status, result, error = "done", None, None
        try:
            if command == "restart":
                self.supervisor.request_manager_restart()
                result = {"accepted": True, "action": "restart"}
            elif command == "stop":
                self.supervisor.request_manager_stop()
                result = {"accepted": True, "action": "stop"}
            elif command == "start":
                self.supervisor.request_manager_start()
                result = {"accepted": True, "action": "start"}
            elif command == "status":
                result = self.telemetry.last_snapshot or {"status": "starting"}
            elif command == "transcript":
                result = {"messages": self._read_transcript(args.get("session"))}
            elif command == "roster":
                result = {"subagents": self._read_roster()}
            elif command == "spend":
                result = {"spend": self._read_spend()}
            elif command == "session_log":
                result = self._read_session_log()
        except Exception as e:  # a request setter/read should never raise, but be safe
            status, error = "error", str(e)
            log.exception("supervisor: admin command %s failed", command)
        self._publish_result(command_id, status, result, error)

    # --- tier-3 reads + data-plane chat (Phase C, #11) --------------------
    #
    # All three answer from the LOCAL $BOBI_HOME via the same public `bobi`
    # surfaces the local web UI (`bobi.webapp.runtime.LocalRuntime`) uses - the
    # sidecar shares the manager's host, state dir, and Claude config. One code
    # path per operation, matching the local UI exactly.

    def _manager_session(self) -> str:
        from bobi import service
        return service.manager_session_name(self.project_root)

    def _read_transcript(self, session) -> list:
        """A session's transcript messages, Claude transcript first then the
        web-UI chat-log fallback - the same read ``LocalRuntime.messages`` does.
        A bad session id raises (as it does locally); the dispatch's outer guard
        turns that into an error result rather than an empty transcript."""
        from bobi.chat_history import read_chat, read_transcript_messages
        from bobi.sdk import load_session_id

        target = (session or "").strip() or self._manager_session()
        messages = read_transcript_messages(
            load_session_id(target, root=self.project_root))
        if not messages:
            messages = read_chat(self.project_root, target)
        return messages

    def _read_roster(self) -> list:
        """The team's session roster, manager first - the same serialization
        the local UI renders (`serialize_subagent`), so one card shape."""
        from bobi import service
        from bobi.webapp.runtime import ordered_subagents, serialize_subagent

        mgr = service.manager_session_name(self.project_root)
        entries = service.list_agents(self.project_root)
        return [serialize_subagent(e, manager_name=mgr)
                for e in ordered_subagents(entries, manager_name=mgr)]

    def _read_spend(self) -> dict:
        """This team's cumulative spend, folded from the local session state
        files - the same read ``LocalRuntime.spend_summary`` does. A cheap file
        fold, so it runs synchronously on the dispatch worker (unlike chat).
        ``sessions_path`` (not ``sessions_dir``) so a read never mkdirs."""
        from bobi import paths
        from bobi.costs import rollup_costs

        return rollup_costs(paths.sessions_path(self.project_root)).to_dict()

    def _read_session_log(self) -> dict:
        """The team's whole session history with honest terminal outcomes -
        the same fold ``LocalRuntime.session_log`` does (#733 vertical 3),
        via the same public helpers, with one hosted delta: the row list is
        capped for transport (``MAX_SESSION_LOG_ROWS``, newest first) and
        ``truncated`` flags the cap. ``counts`` is computed PRE-cap so the
        totals stay honest.

        ``list_all(reap_dead=True)`` runs the dead-pid crash marking and
        returns the reaped view in the same snapshot, so the history never
        shows a dead session as running. This originally shipped as an
        inlined copy of the fold (repo-boundary sequencing: this repo merged
        before the helpers reached the ``dev`` channel); collapsed onto the
        public helpers per the consumer repo's follow-up note.
        """
        from bobi import service
        from bobi.sdk import SessionRegistry
        from bobi.webapp.runtime import (
            ordered_session_log,
            serialize_session,
            session_outcome_counts,
        )

        mgr = service.manager_session_name(self.project_root)
        entries = ordered_session_log(
            SessionRegistry(self.project_root).list_all(reap_dead=True))
        return {
            "sessions": [serialize_session(e, manager_name=mgr)
                         for e in entries[:MAX_SESSION_LOG_ROWS]],
            "counts": session_outcome_counts(entries),
            "truncated": len(entries) > MAX_SESSION_LOG_ROWS,
        }

    def _dispatch_chat_async(self, command_id: str, args: dict) -> None:
        """Run ``service.ask`` on a detached thread; that thread publishes the
        single command result (``done`` when the reply lands in the transcript,
        else ``error``). Always resolves the command - never leaks pending."""
        session = (args.get("session") or "").strip()
        text = (args.get("text") or "").strip()

        def work() -> None:
            status, result, error = "done", None, None
            try:
                if not text:
                    raise ValueError("empty chat text")
                from bobi import service
                target = session or self._manager_session()
                service.ask(self.project_root, target, text)
                result = {"delivered": True, "session": target}
            except Exception as e:  # noqa: BLE001 - the command must resolve
                status, error = "error", str(e)
                log.exception("supervisor: chat command failed")
            self._publish_result(command_id, status, result, error)

        threading.Thread(target=work, daemon=True,
                         name=f"admin-chat-{command_id[:8]}").start()

    def _publish_result(self, command_id: str, status: str, result, error) -> None:
        payload = {
            "deployment": self.identity,
            "command_id": command_id,
            "status": status,
        }
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        try:
            self._publish_fn(COMMAND_RESULT_TOPIC, "fleet", payload,
                             self.project_root)
        except Exception:
            log.debug("supervisor: command_result publish failed", exc_info=True)

    # --- shutdown ---------------------------------------------------------

    def stop(self) -> None:
        client = self._client
        if client is not None:
            try:
                client.stop()
            except Exception:
                log.debug("supervisor: admin listener stop failed", exc_info=True)
        worker = self._worker
        if worker is not None:
            self._work.put(_STOP)
            worker.join(timeout=5)
