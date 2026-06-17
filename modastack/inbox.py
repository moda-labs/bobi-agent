"""Session inbox — in-memory queue fed by the event server.

Every session has an inbox: an in-memory queue its run loop drains. Messages
arrive as ``inbox/<session>`` events on the configured event server, are
delivered over the session's subscription/drain path (the same path lifecycle
events use), and pushed into this queue by the drain loop (see
``events/drain.py``). There is no per-session HTTP server — the inbox is
purely in-process state; the transport is the event server.

``deliver()`` publishes an ``inbox/<target>`` event. For ``wait=True`` it
blocks on a transitional file-based reply rendezvous (a one-shot CLI ``ask``
is a separate process from its target, so the reply can't be held on an
in-process handle). That rendezvous is replaced by reply-topic request/reply
in #269; the ``deliver()`` signature is frozen so call sites don't change.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Poll cadence for the transitional wait=True reply rendezvous (see below).
_REPLY_POLL_INTERVAL = 0.2

# Correlation ids are server-routed wire input (drain reconstructs them from the
# event payload), so they must never be trusted as a filename component. Only
# ids matching the _msg_id() shape are allowed near the replies directory.
_SAFE_CORR_ID = re.compile(r"^[0-9a-f]{1,16}-[0-9a-f]{1,32}$")


def _msg_id() -> str:
    ts = int(time.time() * 1000)
    return f"{ts:013x}-{secrets.token_hex(4)}"


@dataclass
class Message:
    id: str
    sender: str
    text: str
    wait: bool = False


# ---------------------------------------------------------------------------
# Process-local inbox registry
# ---------------------------------------------------------------------------
#
# A session's drain loop runs in the same process as its Session (one
# event-server deployment == one session by construction). The drain looks up
# the live Inbox by name here and pushes events straight into its queue,
# rather than crossing any transport to reach its own session.

_local_inboxes: dict[str, "Inbox"] = {}
_local_inboxes_lock = threading.Lock()


def register_local_inbox(name: str, inbox: "Inbox") -> None:
    with _local_inboxes_lock:
        _local_inboxes[name] = inbox


def unregister_local_inbox(name: str) -> None:
    with _local_inboxes_lock:
        _local_inboxes.pop(name, None)


def get_local_inbox(name: str) -> "Inbox | None":
    with _local_inboxes_lock:
        return _local_inboxes.get(name)


# ---------------------------------------------------------------------------
# Reply rendezvous (transitional — replaced by reply-topic pub/sub in #269)
# ---------------------------------------------------------------------------
#
# Pub/sub is fire-and-forget, but ``deliver(wait=True)`` must return the
# target's reply. Until #269 lands the proper reply-topic request/reply, the
# target writes its reply to ``<state>/replies/<corr_id>.json`` and the waiting
# sender polls for it. Both processes share one installation root, so the file
# is a shared rendezvous point in both same-process and cross-process cases.


def _replies_dir() -> Path:
    from modastack import paths
    d = paths.state_dir() / "replies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reply_path(corr_id: str) -> Path:
    # corr_id reaches us from the event payload (untrusted). Refuse anything
    # that isn't a well-formed message id so it can't escape the replies dir
    # (e.g. id="../../etc/cron" writing the agent's response outside state/).
    if not _SAFE_CORR_ID.match(corr_id or ""):
        raise ValueError(f"unsafe correlation id: {corr_id!r}")
    return _replies_dir() / f"{corr_id}.json"


def _write_reply(corr_id: str, response: str) -> None:
    """Atomically write a reply for a waiting ``deliver(wait=True)`` caller."""
    try:
        path = _reply_path(corr_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"response": response}))
        tmp.replace(path)
    except (OSError, ValueError) as e:
        log.warning("Failed to write reply for %s: %s", corr_id, e)


class Inbox:
    """In-memory message queue drained by a session's run loop."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self._queue: queue.SimpleQueue[Message] = queue.SimpleQueue()
        self._closed = False

    def start(self) -> None:
        """Make the inbox addressable in-process for its drain loop."""
        register_local_inbox(self.session_name, self)
        log.info(f"Inbox for '{self.session_name}' active")

    def push(self, msg: Message) -> None:
        """Enqueue a message for the session's run loop to pick up."""
        self._queue.put(msg)

    def recv(self, timeout: float = 2.0) -> Message | None:
        """Block until a message arrives. Returns None on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def respond(self, msg_id: str, response: str) -> None:
        """Return a reply for a wait-mode message to its waiting sender.

        Writes the reply to the file rendezvous the sender polls. Replaced by
        reply-topic pub/sub in #269.
        """
        _write_reply(msg_id, response)

    def close(self) -> None:
        """Stop being addressable; drop the queue."""
        self._closed = True
        unregister_local_inbox(self.session_name)


def deliver(
    to: str,
    text: str,
    sender: str = "",
    wait: bool = False,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Deliver a message to a session by name over the event server.

    Publishes an ``inbox/<to>`` event; the target's subscription delivers it to
    its in-process queue. A registry presence-check preserves the historical
    "session not found / dead" failures even though publish itself succeeds with
    no subscriber. If ``wait=True``, blocks on the reply rendezvous until the
    target responds or ``timeout`` elapses.

    Returns (success, response_text). Signature is frozen — call sites
    (cli.py message/ask, subagent handoffs, monitors) must not change.
    """
    from modastack.sdk import get_registry, pid_alive

    entry = get_registry().get(to)
    if not entry:
        return False, f"session '{to}' not found"

    if entry.pid and not pid_alive(entry.pid):
        return False, f"session '{to}' process is dead"

    # A terminal session has torn down its subscription/inbox; publishing to it
    # would succeed but no one would consume it (a wait=True sender would just
    # burn the full timeout). pid_alive isn't enough — one process can outlive a
    # stopped phase session, or a SIGKILL'd pid can be recycled.
    if entry.status in ("stopped", "error", "cancelled", "done"):
        return False, f"session '{to}' is {entry.status}"

    from modastack.events.publish import publish_inbox

    msg_id = _msg_id()
    ok = publish_inbox(to, {
        "id": msg_id,
        "sender": sender,
        "text": text,
        "wait": wait,
    })
    if not ok:
        return False, f"could not publish message to '{to}'"

    if not wait:
        return True, ""

    # Transitional blocking reply via file rendezvous (replaced by #269).
    reply_path = _reply_path(msg_id)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if reply_path.exists():
                try:
                    data = json.loads(reply_path.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {}
                return True, data.get("response", "")
            time.sleep(_REPLY_POLL_INTERVAL)
        return False, f"no response within {timeout}s"
    finally:
        reply_path.unlink(missing_ok=True)
