"""``EventBusRuntime`` - the hosted webapp's ``TeamRuntime`` over the fleet API.

The webapp (`bobi/webapp/`) speaks only to a ``TeamRuntime`` (the #690
seam). ``LocalRuntime`` drives this machine's ``$BOBI_HOME`` directly; this
implementation drives a DEPLOYED fleet through the event-server control
plane instead - the fleet read model (`GET /fleet/status`,
`/fleet/instances/:f/:i`) for status/roster, and the operator command RPC
(`POST .../commands` + poll) for lifecycle, transcripts, and chat.

One webapp codebase, two deployments, each with a fixed binding (see the
``runtime.py`` docstring): the local ``bobi app`` binds ``LocalRuntime``; a
hosted console binds this, supplying its own fleet API URL and operator token
through the constructor. No mode-switching.

Instances stay dark and outbound-only, so EVERY operation crosses the control
plane. Chat/transcript/roster are not new bus protocol: the supervisor executes
them locally against the same ``bobi`` surfaces ``LocalRuntime`` uses and
returns the reply as the command result (see ``bobi/supervisor/admin.py``). This
runtime is the operator-authed HTTP client for that plane.

A deployment is addressed by ``(fleet, instance)``; the webapp's flat team
``name`` encodes the pair as ``fleet~instance`` (tilde is URL-path-safe and does
not occur in fleet slugs or Fly instance ids).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx

from bobi.webapp.runtime import (
    TeamLifecycleError,
    TeamRuntime,
    UnknownRun,
    UnknownTeam,
)

log = logging.getLogger(__name__)

# The tilde delimiter that joins (fleet, instance) into the webapp's flat team
# name. Kept in sync with the frontend only implicitly - the name is opaque to
# the UI, which round-trips it verbatim through the /api/agents/<name> routes.
NAME_DELIM = "~"

DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_COMMAND_TIMEOUT = 30.0
COMMAND_POLL_INTERVAL = 0.5
# fleet_spend fans out one command-RPC per instance; the frontend polls
# /api/fleet/spend every ~4s, so without a cache an N-instance fleet would fan
# out N RPCs several times a second. Serve a folded result for this long before
# re-fanning - spend is cumulative and slow-moving, so a few seconds of lag is
# invisible while the load reduction is large.
DEFAULT_FLEET_SPEND_TTL = 30.0
# The fan-out is serial, so a wedged-but-addressable instance (heartbeat live,
# dispatch worker stuck) would otherwise stall the whole dashboard for the full
# command timeout. A read of already-written cost is cheap, so bound each leg
# tightly: a slow box drops to 0 fast rather than holding up the others.
DEFAULT_FLEET_SPEND_COMMAND_TIMEOUT = 8.0
# The health view renders a handful of trail rows; cap what one response
# carries so a restart-looping box (a 48h trail can hold hundreds of events)
# never bloats the ~5s health poll. Newest first, so the cap keeps the recent
# end.
MAX_HEALTH_LIFECYCLE_EVENTS = 50
# session_log is a cheap read of already-written state, polled by the
# frontend - and a supervisor that PREDATES the command (mid-rollout skew)
# drops it with no reply, so a poll against an old box burns the whole
# timeout. Fail fast like the spend fan-out legs rather than holding webapp
# workers for the default 30s.
DEFAULT_SESSION_LOG_COMMAND_TIMEOUT = 8.0
# The single-agent view's reads (runs / overview / run_details / the detailed
# transcript) are the same shape of bet as session_log: a cheap fold of
# already-written state, polled by an open page, and dropped without a reply
# by any supervisor older than SUPERVISOR_VERSION 0.2.0. Same short bound, for
# the same reason - a page left open against a mid-roll box must not pin a
# webapp worker for 30s per poll.
DEFAULT_VIEW_COMMAND_TIMEOUT = 8.0
# The three writes are NOT reads: they are one-shot operator actions, not
# polled, and resume spawns a process before replying. Give them the default
# command budget rather than the tight read bound - a write that times out
# leaves the operator unsure whether it took effect, which is the one outcome
# worth spending seconds to avoid.
DEFAULT_RUN_WRITE_COMMAND_TIMEOUT = DEFAULT_COMMAND_TIMEOUT
# The supervisor version that first answers the six single-agent-view commands
# (bobi/supervisor/snapshot.py). Below this, an instance drops them silently -
# so the runtime reads the heartbeat's version to say "too old" rather than
# blaming the network for a box that is working perfectly.
MIN_VIEW_SUPERVISOR_VERSION = (0, 2, 0)


def encode_name(fleet: str, instance: str) -> str:
    return f"{fleet}{NAME_DELIM}{instance}"


def decode_name(name: str) -> tuple[str, str]:
    fleet, sep, instance = name.partition(NAME_DELIM)
    if not sep or not fleet or not instance:
        raise UnknownTeam(name)
    return fleet, instance


def _seg(s: str) -> str:
    """Percent-encode one path segment (server-side ``decodeSegments`` inverts
    it); ``safe=""`` so a slash in an id cannot escape the segment."""
    return quote(s, safe="")


def _err_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or "")
    except Exception:
        return ""


def _is_running(reachability, status) -> bool:
    """The card/health "running" verdict, in ONE place: the manager is up AND
    we can currently see it. A stopped, probe-confirmed-dead ("down"), or
    unreachable instance is not running; a wedged one still is (stuck, not
    down). The status vocabulary is the sidecar probe's (bobi/supervisor/probe.py):
    starting/running/idle/wedged/down, plus terminal strings echoed from
    /health."""
    return (reachability in ("live", "stale")
            and status not in (None, "stopped", "exited", "down"))


def _version_tuple(raw) -> tuple[int, ...] | None:
    """A dotted version string as a comparable tuple; None if unreadable.

    Unreadable includes absent: a heartbeat from a sidecar too old to stamp a
    version is exactly the case this comparison exists to catch, so it must
    not read as "new enough".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = []
    for chunk in raw.strip().split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def _pid(v) -> int:
    """A heartbeat pid as an int; 0 for missing or malformed (one bad field
    in a snapshot must not 500 the card or health read)."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class EventBusRuntime(TeamRuntime):
    """Drives a deployed fleet through the operator-authed ``/fleet`` API."""

    def __init__(self, *, fleet_api_url: str, operator_token: str,
                 command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
                 poll_interval: float = COMMAND_POLL_INTERVAL,
                 http_timeout: float = DEFAULT_HTTP_TIMEOUT,
                 fleet_spend_ttl: float = DEFAULT_FLEET_SPEND_TTL,
                 fleet_spend_command_timeout: float = (
                     DEFAULT_FLEET_SPEND_COMMAND_TIMEOUT),
                 session_log_command_timeout: float = (
                     DEFAULT_SESSION_LOG_COMMAND_TIMEOUT),
                 view_command_timeout: float = DEFAULT_VIEW_COMMAND_TIMEOUT,
                 run_write_command_timeout: float = (
                     DEFAULT_RUN_WRITE_COMMAND_TIMEOUT),
                 client: httpx.Client | None = None) -> None:
        self._base = fleet_api_url.rstrip("/")
        self._auth = {"authorization": f"Bearer {operator_token}"}
        self._cmd_timeout = command_timeout
        self._poll_interval = poll_interval
        self._client = client or httpx.Client(timeout=http_timeout)
        # (monotonic_ts, folded_result) for the fleet_spend fan-out; None until
        # the first fan-out. ttl <= 0 disables the cache (every call re-fans).
        self._fleet_spend_ttl = fleet_spend_ttl
        self._fleet_spend_command_timeout = fleet_spend_command_timeout
        self._session_log_command_timeout = session_log_command_timeout
        self._view_command_timeout = view_command_timeout
        self._run_write_command_timeout = run_write_command_timeout
        self._fleet_spend_cache: tuple[float, dict] | None = None

    # --- HTTP + command plumbing ------------------------------------------

    def _get(self, path: str) -> httpx.Response:
        return self._client.get(self._base + path, headers=self._auth)

    def _get_detail(self, fleet: str, instance: str) -> dict:
        r = self._get(f"/fleet/instances/{_seg(fleet)}/{_seg(instance)}")
        if r.status_code == 404:
            raise UnknownTeam(encode_name(fleet, instance))
        r.raise_for_status()
        return r.json()

    def _issue_command(self, fleet: str, instance: str, command: str,
                       args: dict | None = None) -> str:
        """POST an operator command; return its ``command_id``. Maps the server's
        addressability/liveness rejections to the typed runtime errors."""
        path = f"/fleet/instances/{_seg(fleet)}/{_seg(instance)}/commands"
        r = self._client.post(self._base + path, headers=self._auth,
                              json={"command": command, "args": args or {}})
        if r.status_code == 404:
            raise UnknownTeam(encode_name(fleet, instance))
        if r.status_code in (409, 503):
            # 409 not-addressable (no heartbeat yet) / 503 no live supervisor:
            # both mean "cannot act on this instance right now".
            raise TeamLifecycleError(
                _err_message(r) or "instance is not reachable")
        r.raise_for_status()
        return str(r.json()["command_id"])

    def _await_command(self, fleet: str, instance: str, command_id: str,
                       timeout: float):
        """Poll a command's result until done/error/timeout; return its
        ``result`` payload, or raise ``TeamLifecycleError``."""
        path = (f"/fleet/instances/{_seg(fleet)}/{_seg(instance)}"
                f"/commands/{_seg(command_id)}")
        deadline = time.monotonic() + timeout
        while True:
            r = self._get(path)
            if r.status_code == 200:
                view = r.json()
                status = view.get("status")
                if status == "done":
                    return view.get("result")
                if status == "error":
                    message = view.get("error") or "command failed"
                    # The supervisor puts a machine-readable `code` on the
                    # result of a refusal it blames the caller for, so a
                    # vanished run maps to the same typed error the local
                    # runtime raises instead of being flattened into prose.
                    detail = view.get("result")
                    code = (detail or {}).get("code") if isinstance(
                        detail, dict) else None
                    if code == "unknown_run":
                        raise UnknownRun(str(detail.get("run_id") or ""))
                    raise TeamLifecycleError(message)
            elif r.status_code != 404 and r.status_code < 500:
                # A 4xx other than 404 (bad auth / bad request) is a real,
                # non-transient error - surface it now. A 404 (result not folded
                # yet) or a 5xx (edge/KV blip) is transient: keep polling to the
                # deadline rather than abort an in-flight command.
                r.raise_for_status()
            if time.monotonic() >= deadline:
                raise TeamLifecycleError(f"command timed out after {timeout:.0f}s")
            time.sleep(self._poll_interval)

    def _command(self, fleet: str, instance: str, command: str,
                 args: dict | None = None, timeout: float | None = None):
        cid = self._issue_command(fleet, instance, command, args)
        return self._await_command(fleet, instance, cid,
                                   timeout or self._cmd_timeout)

    def _supervisor_version(self, fleet: str, instance: str):
        """The instance's sidecar version off the heartbeat, or None.

        Best-effort and never fatal: this only ever refines an error message,
        so a read that fails leaves the caller with the honest generic one.
        """
        try:
            supervisor = self._get_detail(fleet, instance).get("supervisor")
        except Exception:
            return None
        if not isinstance(supervisor, dict):
            return None
        return _version_tuple(supervisor.get("version"))

    def _view_command(self, fleet: str, instance: str, command: str,
                      args: dict | None = None,
                      timeout: float | None = None):
        """Issue one of the single-agent-view commands, and say the true thing
        when it does not come back.

        A supervisor older than ``MIN_VIEW_SUPERVISOR_VERSION`` drops an
        unknown verb WITHOUT a reply (docs/ADMIN_PROTOCOL.md), so the only
        symptom available here is a timeout - indistinguishable, at that
        moment, from a wedged dispatch worker. The fleet is not uniform
        mid-roll, and reporting a working-but-old box as a timeout sends
        whoever is on the page hunting a network fault that does not exist.

        So on timeout, and only then, consult the heartbeat's supervisor
        version: if it predates the command, report it as unavailable and NAME
        the instance. Otherwise the original timeout stands - guessing "too
        old" for a box that is merely stuck would be the same lie inverted.
        """
        try:
            return self._command(fleet, instance, command, args,
                                 timeout=timeout
                                 or self._view_command_timeout)
        except TeamLifecycleError as e:
            if "timed out" not in str(e):
                raise
            version = self._supervisor_version(fleet, instance)
            if version is not None and version >= MIN_VIEW_SUPERVISOR_VERSION:
                raise
            seen = ".".join(str(p) for p in version) if version else "unknown"
            wanted = ".".join(str(p) for p in MIN_VIEW_SUPERVISOR_VERSION)
            raise TeamLifecycleError(
                f"'{command}' is unavailable on "
                f"{encode_name(fleet, instance)}: its supervisor reports "
                f"version {seen}, and this command needs {wanted}. "
                f"The instance is running - it is the sidecar that is older "
                f"than this console.") from None

    # --- card shaping -----------------------------------------------------

    @staticmethod
    def _describe(versions: dict, deployment: dict) -> str:
        bits = []
        team = versions.get("team_package")
        if team:
            bits.append(f"team {team}")
        where = deployment.get("region") or deployment.get("platform")
        if where:
            bits.append(str(where))
        return " - ".join(bits)

    def _card(self, entry: dict) -> dict:
        """A dashboard card from a fleet-status entry or an instance detail
        (both expose deployment/manager/reachability/versions at the top level)."""
        d = entry.get("deployment") or {}
        mgr = entry.get("manager") or {}
        reach = entry.get("reachability")
        status = mgr.get("status")
        # reachability/manager_status ride along for the hosted dashboard to
        # render live/stale/unreachable/stopped distinctly (the existing UI
        # ignores unknown fields).
        return {
            "name": encode_name(d.get("fleet"), d.get("instance")),
            "installed": True,
            "running": _is_running(reach, status),
            "pid": _pid(mgr.get("pid")),
            "description": self._describe(entry.get("versions") or {}, d),
            "reachability": reach,
            "manager_status": status,
        }

    # --- TeamRuntime surface ----------------------------------------------

    def dashboard(self) -> dict:
        r = self._get("/fleet/status")
        r.raise_for_status()
        instances = r.json().get("instances") or []
        return {"agents": [self._card(e) for e in instances], "home": "fleet"}

    def team_status(self, name: str) -> dict:
        fleet, instance = decode_name(name)
        return self._card(self._get_detail(fleet, instance))

    # A lifecycle command only ACKs; the manager's new pid surfaces on the next
    # heartbeat, which the card re-poll picks up. So report pid 0 (unknown-yet)
    # rather than a second round-trip that would read a stale pre-effect pid.
    def start_team(self, name: str) -> dict:
        fleet, instance = decode_name(name)
        self._command(fleet, instance, "start")
        return {"ok": True, "pid": 0}

    def stop_team(self, name: str) -> dict:
        fleet, instance = decode_name(name)
        self._command(fleet, instance, "stop")
        return {"ok": True, "stopped": True, "pid": 0, "still_running": False}

    def restart_team(self, name: str) -> dict:
        fleet, instance = decode_name(name)
        self._command(fleet, instance, "restart")
        return {"ok": True, "pid": 0}

    def subagents(self, name: str) -> list[dict]:
        # One path, like LocalRuntime: the roster command returns the rich
        # serialization; an unreachable supervisor raises TeamLifecycleError
        # (mapped to 409 by the handler) rather than silently masking the failure
        # behind a stale heartbeat roster.
        fleet, instance = decode_name(name)
        result = self._command(fleet, instance, "roster")
        return list((result or {}).get("subagents") or [])

    def messages(self, name: str, session: str) -> list[dict]:
        fleet, instance = decode_name(name)
        result = self._command(fleet, instance, "transcript",
                               {"session": session})
        return list((result or {}).get("messages") or [])

    def transcript(self, name: str, session: str) -> dict:
        """The debugging view of a session's transcript.

        Rides the EXISTING ``transcript`` command with ``detail: true`` rather
        than a new verb: it is the same file, for the same session, and the
        supervisor widens its reply instead of answering twice. It cannot be
        built by reshaping ``messages`` - that list is the chat view, tool
        calls already discarded - so a supervisor that answers without
        ``entries`` is one that predates the arg, and that is reported as
        unavailable rather than rendered as a transcript with every tool call
        mysteriously missing.
        """
        fleet, instance = decode_name(name)
        result = self._view_command(fleet, instance, "transcript",
                                    {"session": session, "detail": True}) or {}
        if "entries" not in result:
            raise TeamLifecycleError(
                f"the debugging transcript is unavailable on "
                f"{encode_name(fleet, instance)}: its supervisor answered the "
                f"chat view only, which carries no tool calls. It needs "
                f"supervisor "
                f"{'.'.join(str(p) for p in MIN_VIEW_SUPERVISOR_VERSION)}.")
        return {
            "session": result.get("session") or session,
            "entries": list(result.get("entries") or []),
            "usage": result.get("usage") or {},
        }

    def run_details(self, name: str, run_id: str) -> dict:
        fleet, instance = decode_name(name)
        result = self._view_command(fleet, instance, "run_details",
                                    {"run_id": run_id}) or {}
        return result.get("details") or {}

    def overview(self, name: str) -> dict:
        fleet, instance = decode_name(name)
        result = self._view_command(fleet, instance, "overview") or {}
        return result.get("overview") or {}

    def runs(self, name: str, *, status: str = "", query: str = "",
             offset: int = 0, limit: int | None = None) -> dict:
        """The unified runs table for one hosted agent.

        The box folds it (same ``build_runs`` the local runtime calls) and
        this returns the page whole. Defaults are filled in HERE rather than
        left to the box so the response shape does not depend on which
        supervisor answered - a caller reading ``offset``/``limit`` back off
        the payload gets the numbers it asked for either way.
        """
        fleet, instance = decode_name(name)
        args = {"status": status or "", "query": query or "",
                "offset": max(0, offset)}
        if limit and limit > 0:
            args["limit"] = limit
        result = self._view_command(fleet, instance, "runs", args) or {}
        return {
            "runs": list(result.get("runs") or []),
            "counts": result.get("counts") or {},
            "total": int(result.get("total") or 0),
            "offset": int(result.get("offset") or 0),
            "limit": int(result.get("limit") or 0),
            "query": result.get("query") or "",
            "truncated": bool(result.get("truncated")),
        }

    # The three writes. Unlike the reads above they are one-shot operator
    # actions, so they take the full command budget (see
    # DEFAULT_RUN_WRITE_COMMAND_TIMEOUT) - and `resume` returns `accepted`,
    # never holding the request open for the workflow itself.
    def resume_run(self, name: str, run_id: str) -> dict:
        return self._run_write(name, "resume_run", run_id)

    def remind_run(self, name: str, run_id: str) -> dict:
        return self._run_write(name, "remind_run", run_id)

    def close_run(self, name: str, run_id: str) -> dict:
        return self._run_write(name, "close_run", run_id)

    def _run_write(self, name: str, command: str, run_id: str) -> dict:
        fleet, instance = decode_name(name)
        result = self._view_command(
            fleet, instance, command, {"run_id": run_id},
            timeout=self._run_write_command_timeout)
        return dict(result or {})

    def chat_submit(self, name: str, session: str, text: str) -> str:
        # The command_id IS the message id: the turn runs async on the box (the
        # supervisor's detached worker resolves the command when the reply lands
        # in the transcript), so submit does not block.
        fleet, instance = decode_name(name)
        return self._issue_command(fleet, instance, "chat",
                                   {"session": session, "text": text})

    def chat_job(self, name: str, message_id: str) -> dict | None:
        fleet, instance = decode_name(name)
        path = (f"/fleet/instances/{_seg(fleet)}/{_seg(instance)}"
                f"/commands/{_seg(message_id)}")
        r = self._get(path)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        view = r.json()
        # Only a chat command is a chat job (defense: a non-chat id is "not
        # mine"). `command` is null when only a result folded - tolerate that.
        if view.get("command") not in ("chat", None):
            return None
        status = view.get("status")
        if status == "done":
            return {"status": "done"}
        if status == "error":
            return {"status": "error", "error": view.get("error") or "chat failed"}
        return {"status": "pending"}

    # --- observability (read-only, #733) --------------------------------
    # The supervisor folds spend from its LOCAL $BOBI_HOME via the same
    # bobi.costs.rollup_costs LocalRuntime uses and returns it as the command
    # result; this runtime is just the command-RPC client. Cumulative figures
    # (they reset only on a state wipe).

    def health_summary(self, name: str) -> dict:
        """One team's system health, straight off the fleet read model: the
        instance detail already folds reachability (heartbeat age), the
        sidecar's manager summary, session statuses, and the 48h lifecycle
        trail. Unlike spend there is NO command-RPC leg - the box itself is
        never consulted, so a stopped, wedged, or unreachable instance still
        reports (its silence IS the signal), and no cache is needed (one KV
        read per poll)."""
        fleet, instance = decode_name(name)
        detail = self._get_detail(fleet, instance)
        mgr = detail.get("manager") or {}
        reach = detail.get("reachability")
        status = mgr.get("status")
        lifecycle = []
        for entry in (detail.get("lifecycle")
                      or [])[:MAX_HEALTH_LIFECYCLE_EVENTS]:
            if not isinstance(entry, dict):
                continue
            lifecycle.append({
                "event": entry.get("event"),
                # `at` is the box's own clock; `received_at` (server receipt,
                # epoch ms) is authoritative and always present.
                "at": entry.get("generated_at"),
                "received_at": entry.get("received_at"),
                "status": entry.get("status"),
                "reason": entry.get("reason"),
            })
        return {
            "reachability": reach,
            "last_heartbeat_at": detail.get("last_heartbeat_at"),
            "manager": {
                # A heartbeat with no manager block (a very old sidecar) has
                # no verdict to report - "unknown", never a fabricated one.
                "status": status or "unknown",
                "pid": _pid(mgr.get("pid")),
                "running": _is_running(reach, status),
                # The probe verdict AS OF the last heartbeat; reachability
                # rides along so a stale verdict is visibly stale.
                "healthy": mgr.get("healthy"),
                "restart_count": mgr.get("restart_count"),
                "last_restart_reason": mgr.get("last_restart_reason"),
                "last_restart_at": mgr.get("last_restart_at"),
                "idle_seconds": mgr.get("idle_seconds"),
            },
            "sessions": [
                {"name": s.get("name"), "role": s.get("role"),
                 "status": s.get("status")}
                for s in (detail.get("sessions") or [])
                if isinstance(s, dict)
            ],
            "lifecycle": lifecycle,
        }

    def session_log(self, name: str) -> dict:
        """One team's session history with terminal outcomes (#733 vertical
        3): one box-consulting command-RPC, like ``spend_summary`` - the full
        history lives only on the box. An unreachable supervisor raises
        ``TeamLifecycleError`` (mapped to 409) rather than masking with an
        empty log. No TTL cache and no fan-out: the spend cache exists for the
        N-box dashboard fold, and sessions have no fleet-wide surface. The
        box caps ``sessions`` (``MAX_SESSION_LOG_ROWS``, newest first) and
        flags it via ``truncated``; ``counts`` covers the whole history.
        The short timeout keeps a mid-rollout box whose supervisor predates
        the command (it drops unknown verbs with no reply) from pinning a
        webapp worker for the default command timeout on every poll."""
        fleet, instance = decode_name(name)
        result = self._command(fleet, instance, "session_log",
                               timeout=self._session_log_command_timeout) or {}
        return {
            "sessions": list(result.get("sessions") or []),
            "counts": result.get("counts") or {},
            "truncated": bool(result.get("truncated")),
        }

    def _fetch_spend(self, fleet, instance, timeout: float | None = None) -> dict:
        """One team's spend envelope, unwrapped. Shared by ``spend_summary``
        (per-agent view) and the ``fleet_spend`` fan-out so the ``{"spend": ...}``
        wire shape and the raise-on-unreachable policy live in one place. Bare
        ``{}`` on a missing payload, exactly as ``subagents``/``messages`` fall
        back to ``[]``."""
        result = self._command(fleet, instance, "spend", timeout=timeout)
        return (result or {}).get("spend") or {}

    def spend_summary(self, name: str) -> dict:
        # One path, like subagents()/messages(): an unreachable supervisor
        # raises TeamLifecycleError (mapped to 409) rather than masking with
        # zeros. The CostSummary.to_dict() shape comes straight from the box.
        fleet, instance = decode_name(name)
        return self._fetch_spend(fleet, instance)

    def fleet_spend(self) -> dict:
        """Fleet-wide spend for the dashboard header + per-tile totals.

        No spend field rides the heartbeat read model (that fold is deferred,
        see #733), so this fans out one ``spend`` command-RPC per live instance
        and sums the results. A short TTL cache keeps the ~4s frontend poll from
        re-fanning on every request."""
        now = time.monotonic()
        cached = self._fleet_spend_cache
        if (cached is not None
                and now - cached[0] < self._fleet_spend_ttl):
            return cached[1]
        result = self._fleet_spend_uncached()
        self._fleet_spend_cache = (now, result)
        return result

    def _fleet_spend_uncached(self) -> dict:
        r = self._get("/fleet/status")
        r.raise_for_status()
        instances = r.json().get("instances") or []
        total = 0.0
        estimated = 0.0
        counted = 0
        teams: list[dict] = []
        for entry in instances:
            d = entry.get("deployment") or {}
            fleet, instance = d.get("fleet"), d.get("instance")
            team_total, team_estimated, sessions = 0.0, 0.0, 0
            try:
                spend = self._fetch_spend(
                    fleet, instance,
                    timeout=self._fleet_spend_command_timeout)
                # Round the per-team totals so the header equals the sum of the
                # visible tiles (mirrors LocalRuntime.fleet_spend - no sub-cent
                # drift where the header shows spend no tile accounts for).
                team_total = round(float(spend.get("total_cost_usd") or 0.0), 4)
                # Estimated rides separately, never mixed into recorded dollars
                # (#760: a pre-0.44 box reports no estimate key, folding as 0).
                team_estimated = round(
                    float(spend.get("estimated_cost_usd") or 0.0), 4)
                sessions = int(spend.get("sessions_counted") or 0)
            except Exception:
                # Fault isolation: ANY per-box failure (unreachable/wedged ->
                # TeamLifecycleError; de-registered mid-fan-out -> UnknownTeam;
                # an edge/HTTP blip; a malformed identity) contributes 0 but the
                # box still lists, so one dead box never fails the whole
                # dashboard. Logged, not swallowed silently.
                log.warning("fleet_spend: skipping %s~%s", fleet, instance,
                            exc_info=True)
            teams.append({
                "name": encode_name(fleet, instance),
                "total_cost_usd": team_total,
                "estimated_cost_usd": team_estimated,
                "sessions_counted": sessions,
            })
            total += team_total
            estimated += team_estimated
            counted += sessions
        teams.sort(key=lambda t: (t["total_cost_usd"]
                                  + t["estimated_cost_usd"]), reverse=True)
        return {
            "total_cost_usd": round(total, 4),
            "estimated_cost_usd": round(estimated, 4),
            "sessions_counted": counted,
            "teams": teams,
        }
