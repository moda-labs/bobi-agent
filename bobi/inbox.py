"""Session inbox — in-memory queue fed by the event server.

Every session has an inbox: an in-memory queue its run loop drains. Messages
arrive as ``inbox/<session>`` events on the configured event server, are
delivered over the session's subscription/drain path (the same path lifecycle
events use), and pushed into this queue by the drain loop (see
``events/drain.py``). There is no per-session HTTP server — the inbox is
purely in-process state; the transport is the event server.

``deliver()`` publishes an ``inbox/<target>`` event. For ``wait=True`` it
becomes async request/reply correlated on an id (#269): the sender opens a
transient ``reply/<uuid>`` subscription, publishes the request carrying that
topic as ``reply_to``, and awaits the reply event whose ``corr_id`` matches.
The target session replies by publishing ``{corr_id, response}`` to
``reply_to`` (``Inbox.respond``). One pub/sub transport, no files; works
identically for a one-shot CLI ``ask`` (a separate process from its target)
and any in-process caller. The ``deliver()`` signature is frozen so call
sites don't change.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def _msg_id() -> str:
    ts = int(time.time() * 1000)
    return f"{ts:013x}-{secrets.token_hex(4)}"


@dataclass
class Message:
    id: str
    sender: str
    text: str
    wait: bool = False
    # Topic to publish the reply to when ``wait`` is set (#269). Empty for
    # fire-and-forget messages. The request's ``id`` is the correlation id the
    # reply carries back.
    reply_to: str = ""
    # Completion callback used by the event drain to ACK the event-server cursor
    # only after the session has actually processed this inbox message.
    ack: Callable[[], None] | None = field(default=None, repr=False, compare=False)


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


class Inbox:
    """In-memory message queue drained by a session's run loop."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self._queue: queue.PriorityQueue[tuple[int, int, Message]] = queue.PriorityQueue()
        self._counter = 0
        self._closed = False

    def start(self) -> None:
        """Make the inbox addressable in-process for its drain loop."""
        register_local_inbox(self.session_name, self)
        log.info(f"Inbox for '{self.session_name}' active")

    def push(self, msg: Message, priority: bool = False) -> None:
        """Enqueue a message for the session's run loop to pick up."""
        self._counter += 1
        # Lower priority values are returned first. The counter preserves FIFO
        # ordering within chat and normal classes.
        self._queue.put((0 if priority else 1, self._counter, msg))

    def recv(self, timeout: float = 2.0) -> Message | None:
        """Block until a message arrives. Returns None on timeout."""
        try:
            return self._queue.get(timeout=timeout)[2]
        except queue.Empty:
            return None

    def respond(self, msg: "Message", response: str) -> None:
        """Return a reply for a wait-mode message to its waiting sender.

        Publishes ``{corr_id, response}`` to the message's ``reply_to`` topic,
        where the blocking sender (a transient ``reply/<uuid>`` subscriber) is
        waiting. No-ops for a message with no ``reply_to`` (a fire-and-forget
        message, or one from a sender that didn't ask to wait).

        ``reply_to`` is untrusted wire input, so only a dedicated ``reply/``
        topic is honored — never an arbitrary topic a crafted request could
        name to redirect this agent's response into another session's inbox or
        a broadcast topic. ``deliver()`` only ever sets ``reply/<uuid>``.
        """
        if not msg.id or not msg.reply_to.startswith("reply/"):
            return
        from bobi.events.publish import publish_reply
        if not publish_reply(msg.reply_to, msg.id, response):
            # The work is done but the reply didn't go out (server down,
            # rejected). The waiting sender will time out — log it so the
            # mismatch between "turn completed" and "ask timed out" is traceable.
            log.warning("Failed to publish reply for %s to %s — waiting sender "
                        "will time out despite this turn completing",
                        msg.id, msg.reply_to)

    def close(self) -> None:
        """Stop being addressable; drop the queue."""
        self._closed = True
        unregister_local_inbox(self.session_name)


# ---------------------------------------------------------------------------
# Transient reply channel (the wait=True sender side)
# ---------------------------------------------------------------------------
#
# Pub/sub is fire-and-forget, so a blocking ``deliver(wait=True)`` must
# subscribe to hear the reply. The sender (commonly a one-shot ``bobi
# ask`` process, with no standing subscription) registers a throwaway
# deployment subscribed to a unique ``reply/<uuid>`` topic, publishes the
# request with ``reply_to=reply/<uuid>``, and reads the reply off the
# subscription's queue. The reply is durable in the server's per-deployment
# buffer, so a late WS connect just replays it — no connect race to lose.


@dataclass
class _ReplyChannel:
    client: "object"
    queue: "queue.SimpleQueue"
    topic: str
    cursor_path: Path

    def wait_connected(self, timeout: float) -> bool:
        """Block until the subscription's WS is live (subscribe-before-publish)."""
        return self.client.wait_connected(timeout)  # type: ignore[attr-defined]

    def close(self) -> None:
        try:
            self.client.stop()  # type: ignore[attr-defined]
        except Exception:
            log.debug("Reply channel client stop failed", exc_info=True)
        # Deregister the throwaway deployment server-side so it doesn't leak.
        try:
            from bobi.events.server import deregister
            deregister(
                self.client.server_url,  # type: ignore[attr-defined]
                self.client.deployment_id,  # type: ignore[attr-defined]
                self.client.api_key,  # type: ignore[attr-defined]
            )
        except Exception:
            log.warning("Reply channel deregister failed — server-side "
                        "deployment may leak", exc_info=True)
        self.cursor_path.unlink(missing_ok=True)
        # The shared EventServerClient also writes a per-deployment events log
        # (events/client.py _log_event). For a throwaway reply channel that's
        # pure litter — drop it so asks don't accumulate state/ files.
        try:
            from bobi import paths
            deployment_id = self.client.deployment_id  # type: ignore[attr-defined]
            (paths.state_dir() / f"events-{deployment_id}.jsonl").unlink(missing_ok=True)
        except Exception:
            log.debug("Reply channel events-log cleanup failed", exc_info=True)


def _open_reply_channel(project_path: Path) -> "_ReplyChannel | None":
    """Subscribe to a fresh ``reply/<uuid>`` topic and return its channel.

    Returns None if the event server can't be reached — the caller treats that
    as a publish/transport failure. The transient deployment is deregistered
    server-side by ``_ReplyChannel.close()`` (#277).
    """
    from bobi import paths
    from bobi.events.client import EventServerClient
    from bobi.events.publish import _event_server_url
    from bobi.events.server import BubbleRejected, ensure_bubble, register

    token = secrets.token_hex(8)
    topic = f"reply/{token}"
    es_url = _event_server_url(project_path)
    try:
        # The reply channel MUST join the instance's bubble — otherwise its
        # reply/<uuid> subscription lands in a different bubble than the
        # responder publishes into, and the blocking ask hangs to timeout.
        bubble = ensure_bubble(es_url, project_path)
        try:
            deployment_id, api_key = register(
                es_url, f"reply-{token}", [topic],
                bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
            )
        except BubbleRejected:
            bubble = ensure_bubble(es_url, project_path,
                                   force_remint_of=bubble["bubble_id"])
            deployment_id, api_key = register(
                es_url, f"reply-{token}", [topic],
                bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
            )
    except Exception as e:
        log.warning("Could not open reply channel on %s: %s", es_url, e)
        return None

    q: queue.SimpleQueue = queue.SimpleQueue()
    # A dedicated cursor file: the default (shared) cursor.json belongs to the
    # process's main subscription, and a fresh deployment has its own seq space.
    cursor_path = paths.state_dir() / f"reply-cursor-{token}.json"
    client = EventServerClient(
        server_url=es_url,
        deployment_id=deployment_id,
        api_key=api_key,
        queue=q,
        cursor_path=cursor_path,
    )
    client.start()
    return _ReplyChannel(client=client, queue=q, topic=topic, cursor_path=cursor_path)


def _await_reply(channel: "_ReplyChannel", corr_id: str, deadline: float,
                 timeout: int) -> tuple[bool, str]:
    """Block until a reply with ``corr_id`` lands, or ``deadline`` passes.

    ``deadline`` is an absolute ``time.monotonic()`` value so the connect-wait
    and the reply-wait share one budget; ``timeout`` is only for the message.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"no response within {timeout}s"
        try:
            event = channel.queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        payload = event.get("payload") or {}
        # The deployment only subscribes to its own reply topic, so every event
        # here is a reply for us; the corr_id check guards against a buffered
        # straggler from a reused token (tokens are random — belt-and-suspenders).
        if payload.get("corr_id") == corr_id:
            return True, payload.get("response", "")


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
    no subscriber. If ``wait=True``, opens a transient ``reply/<uuid>``
    subscription, publishes the request carrying it as ``reply_to``, and blocks
    until the target replies (correlated on the message id) or ``timeout``
    elapses.

    Returns (success, response_text). Signature is frozen — call sites
    (cli.py message/ask, subagent handoffs, monitors) must not change.
    """
    from bobi.paths import bobi_root
    from bobi.sdk import get_registry, pid_alive

    entry = get_registry().get(to)
    if not entry:
        return False, f"session '{to}' not found"

    if entry.pid and not pid_alive(entry.pid):
        return False, f"session '{to}' process is dead"

    # A terminal session has torn down its subscription/inbox; publishing to it
    # would succeed but no one would consume it (a wait=True sender would just
    # burn the full timeout). pid_alive isn't enough — one process can outlive a
    # stopped phase session, or a SIGKILL'd pid can be recycled.
    from bobi.sdk import DEAD_STATUSES
    if entry.status in DEAD_STATUSES:
        return False, f"session '{to}' is {entry.status}"

    from bobi.events.publish import publish_inbox

    project_path = bobi_root()
    msg_id = _msg_id()

    if not wait:
        ok = publish_inbox(to, {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "wait": False,
        }, project_path)
        return (True, "") if ok else (False, f"could not publish message to '{to}'")

    # wait=True: open a reply subscription, then publish, then await the
    # correlated reply — all within one timeout budget.
    channel = _open_reply_channel(project_path)
    if channel is None:
        return False, f"could not publish message to '{to}'"
    try:
        deadline = time.monotonic() + timeout
        # Subscribe BEFORE publish, and only publish once the subscription's WS
        # is actually live. A fresh deployment connects with last_seen=0 and the
        # server replays buffered events only when last_seen>0 — so a reply
        # published during the connect window would be buffered but never
        # replayed to us, and we'd hang to the timeout. Waiting for the
        # connected frame makes the reply arrive via live delivery instead.
        if not channel.wait_connected(max(0.0, deadline - time.monotonic())):
            return False, f"no response within {timeout}s"
        ok = publish_inbox(to, {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "wait": True,
            "reply_to": channel.topic,
        }, project_path)
        if not ok:
            return False, f"could not publish message to '{to}'"
        return _await_reply(channel, msg_id, deadline, timeout)
    finally:
        channel.close()
