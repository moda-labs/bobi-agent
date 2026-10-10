"""Subscription-login bootstrap (containerized-23 / #343; brain-aware in #485).

At container first boot in subscription auth mode with no credentials on the
volume, drive the brain's login CLI under a pty, scrape the sign-in URL, post it
to a private chat channel, and land the OAuth credentials on the volume. Two
flow shapes, picked per brain:

- **Claude** (``claude auth login --claudeai``, *paste-back*): scrape the URL,
  post it, wait for the human to paste the auth code back — which arrives as a
  chat message event over the event bus — and write it into the pty.
- **Codex** (``codex login --device-auth``, *device-poll*): scrape the sign-in
  URL **and** the one-time code, post both, then just wait — the CLI polls the
  token endpoint until the human authorizes; nothing is pasted back.

Refresh-token rotation makes this a once-per-machine ceremony
(docs/CONTAINERIZED_DEPLOYMENT.md); the manual fallback is ``fly ssh console`` +
the brain's login command. The live round-trip needs a real event server and is
exercised in the deployed environment; the mechanism here is unit-tested with
the pty, the chat post, and the event source faked — see
tests/test_auth_bootstrap.py.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from bobi.brain import (
    BRAIN_ENV,
    DEFAULT_BRAIN,
    normalize_brain_kind,
    set_process_brain_from_config,
)
from bobi.config import Config
from bobi.conversation import parse_conversation
from bobi.slack import post_slack_message

log = logging.getLogger(__name__)

# Env var naming where to post the login URL. Legacy Slack deployments set a
# raw Slack channel/DM id (``C...``/``D...``). Newer deployments may set the
# channel-gateway conversation ref carried by chat events, e.g.
# ``discord:<application_id>:dm:<channel_id>``.
LOGIN_CHANNEL_ENV = "BOBI_LOGIN_CHANNEL"

# ANSI escape sequences. codex/claude colorize their login output, and a color
# code (e.g. ESC[94m) sits directly before the one-time code — which breaks a
# ``\b`` anchor in the code regex (the trailing 'm' touches the code with no word
# boundary). Strip these before matching the URL/code.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


@dataclass(frozen=True)
class SubscriptionLogin:
    """How one brain performs an interactive subscription login on a headless box."""

    kind: str
    login_cmd: tuple[str, ...]       # the CLI that drives the OAuth flow
    creds_relpath: tuple[str, ...]   # OAuth credential fallback relative to $HOME
    shadow_env: str                  # the API key that would silently outrank subscription auth
    flow: str                        # "paste_back" (claude) | "device_poll" (codex)
    url_re: re.Pattern               # scrape the sign-in URL from pty output
    code_re: re.Pattern | None = None  # device_poll: also scrape the one-time code
    credentials_dir_env: str | None = None


@dataclass(frozen=True)
class CredentialStatus:
    """Local structural validity of a subscription credential file."""

    valid: bool
    reason: str


@dataclass(frozen=True)
class LoginChannel:
    """Destination and subscription details for the bootstrap chat channel.

    ``thread_ts`` is the thread every post after the ask lands in. The gateway
    path threads by appending ``:thread:<ts>`` to the destination ref; the
    legacy Slack path passes it to ``post_slack_message``. One field, so both
    paths thread through one mechanism.
    """

    destination: str
    source: str
    topic: str
    legacy_slack_channel: str = ""
    thread_ts: str = ""


_SPECS: dict[str, SubscriptionLogin] = {
    "claude": SubscriptionLogin(
        kind="claude",
        login_cmd=("claude", "auth", "login", "--claudeai"),
        creds_relpath=(".claude", ".credentials.json"),
        shadow_env="ANTHROPIC_API_KEY",
        flow="paste_back",
        # e.g. https://claude.com/cai/oauth/authorize?code=true&client_id=...
        url_re=re.compile(r"https://\S+/oauth/authorize\S+"),
        credentials_dir_env="CLAUDE_CONFIG_DIR",
    ),
    "codex": SubscriptionLogin(
        kind="codex",
        login_cmd=("codex", "login", "--device-auth"),
        creds_relpath=(".codex", "auth.json"),
        shadow_env="OPENAI_API_KEY",
        flow="device_poll",
        # Codex prints a fixed device URL + a one-time code "XXXX-XXXXX".
        url_re=re.compile(r"https://auth\.openai\.com/codex/device\S*"),
        code_re=re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{5})\b"),
        credentials_dir_env="CODEX_HOME",
    ),
}


def _active_spec() -> SubscriptionLogin:
    """The login spec for this process's brain (``BOBI_BRAIN``; default claude)."""
    kind = os.environ.get(BRAIN_ENV) or "claude"
    return _SPECS.get(kind, _SPECS["claude"])


def subscription_credentials_status(
    path: Path,
    kind: str,
    *,
    now_ms: float | None = None,
) -> CredentialStatus:
    """Check whether a stored OAuth credential can refresh without a network call."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return CredentialStatus(False, "credential file is missing")
    except json.JSONDecodeError:
        return CredentialStatus(False, "credential JSON is malformed")
    except OSError:
        return CredentialStatus(False, "credential file is unreadable")

    if not isinstance(data, dict):
        return CredentialStatus(False, "credential JSON root is not an object")

    if kind == "claude":
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return CredentialStatus(False, "claudeAiOauth is missing or malformed")
        refresh_token = oauth.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return CredentialStatus(False, "refresh token is missing or blank")

        expires_at = oauth.get("refreshTokenExpiresAt")
        if expires_at is None:
            return CredentialStatus(True, "refresh token is present")
        if (isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at)):
            return CredentialStatus(False, "refresh token expiry is malformed")
        current_ms = time.time() * 1000 if now_ms is None else now_ms
        if expires_at <= current_ms:
            return CredentialStatus(False, "refresh token is expired")
        return CredentialStatus(True, "refresh token is present and unexpired")

    if kind == "codex":
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return CredentialStatus(False, "tokens is missing or malformed")
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return CredentialStatus(False, "refresh token is missing or blank")
        # Codex's real OAuth schema has no refresh-token expiry field. An
        # expired access token is recoverable as long as this token is present.
        return CredentialStatus(True, "refresh token is present")

    return CredentialStatus(False, f"unsupported credential schema '{kind}'")


def credentials_path(home: Path | None = None) -> Path:
    """Path to the active brain's subscription OAuth credentials on the volume.

    Claude and Codex each support a provider-specific config directory override.
    Use it when present; otherwise retain the historical ``$HOME``-relative
    fallback used by local callers and tests.
    """
    spec = _active_spec()
    if spec.credentials_dir_env:
        configured_dir = os.environ.get(spec.credentials_dir_env)
        if configured_dir:
            return Path(configured_dir, spec.creds_relpath[-1])
    base = home or Path(os.environ.get("HOME", str(Path.home())))
    return Path(base, *spec.creds_relpath)


def credentials_exist(home: Path | None = None) -> bool:
    """True when the active brain has locally refreshable subscription OAuth."""
    spec = _active_spec()
    return subscription_credentials_status(
        credentials_path(home), spec.kind,
    ).valid


def needs_bootstrap(home: Path | None = None) -> bool:
    """True iff we're in subscription mode with no credentials yet."""
    if os.environ.get("BOBI_AUTH", "api_key") != "subscription":
        return False
    return not credentials_exist(home)


# --- pty driver -------------------------------------------------------------

def _spawn_login(home: Path) -> tuple[subprocess.Popen, int]:
    """Spawn the active brain's login CLI on a pty. Returns (proc, master_fd)."""
    import pty

    spec = _active_spec()
    master, slave = pty.openpty()
    env = dict(os.environ)
    env["HOME"] = str(home)
    # The provider API key silently outranks subscription creds (§6.1) — never
    # let it leak into the login subprocess.
    env.pop(spec.shadow_env, None)
    proc = subprocess.Popen(
        list(spec.login_cmd),
        stdin=slave, stdout=slave, stderr=slave,
        env=env, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    return proc, master


def _scrape_login(
    master_fd: int, timeout: float, spec: SubscriptionLogin
) -> tuple[str, str | None]:
    """Read pty output until the sign-in URL (and, for ``device_poll``, the
    one-time code) appear. Returns ``(url, code|None)``."""
    deadline = time.monotonic() + timeout
    buf = ""
    url: str | None = None
    code: str | None = None
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 1.0)
        if master_fd not in ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        clean = _ANSI_RE.sub("", buf)
        if url is None:
            m = spec.url_re.search(clean)
            if m:
                url = m.group(0)
        if spec.code_re is not None and code is None:
            m = spec.code_re.search(clean)
            if m:
                code = m.group(1)
        if url is not None and (spec.code_re is None or code is not None):
            return url, code
    want = "URL/code" if spec.code_re is not None else "URL"
    raise TimeoutError(
        f"did not see the {spec.kind} login {want} within {timeout:.0f}s"
    )


def _read_until_url(master_fd: int, timeout: float) -> str:
    """Read pty output until the active brain's sign-in URL appears; return it."""
    url, _ = _scrape_login(master_fd, timeout, _active_spec())
    return url


def _write_line(master_fd: int, text: str) -> None:
    os.write(master_fd, (text.strip() + "\n").encode())


# --- event-bus wait ---------------------------------------------------------

def _slack_topic(cfg: Config) -> str:
    from bobi.slack import require_app_identity

    token = cfg.credential("slack", "bot_token")
    team_id, _bot_id, _bot_user_id, app_id = require_app_identity(token)
    return f"slack:{team_id}:app:{app_id}"


def _resolve_login_channel(cfg: Config, raw: str) -> LoginChannel:
    """Resolve ``BOBI_LOGIN_CHANNEL`` to a post destination and bus topic."""
    conv = parse_conversation(raw)
    if conv is None:
        token = cfg.credential("slack", "bot_token")
        if not token:
            raise RuntimeError(
                f"{LOGIN_CHANNEL_ENV} is not a conversation ref, and no Slack "
                "bot_token is configured for the legacy Slack channel-id path."
            )
        from bobi.slack import resolve_channel_id
        channel = resolve_channel_id(token, raw)
        return LoginChannel(
            destination=channel,
            source="slack",
            topic="",
            legacy_slack_channel=channel,
        )

    source, scope = conv.source, conv.scope
    if source == "slack":
        return LoginChannel(destination=raw, source=source, topic=_slack_topic(cfg))
    if source in {"discord", "whatsapp"}:
        if source == "discord" and conv.chat_type == "channel":
            # Under the one correlation rule a channel destination needs a
            # thread anchor, and the Discord adapter emits none: its fields are
            # user_id/user_name/channel_id/message_id/application_id. No inbound
            # event could ever satisfy the predicate, so the run would hang to
            # timeout. Refuse with the gap named. Discord DMs stay supported.
            raise RuntimeError(
                f"{LOGIN_CHANNEL_ENV} points at a Discord guild channel "
                f"({raw}), which cannot be a login destination: a channel "
                "destination is correlated by the reply's thread anchor, and "
                "the Discord adapter does not emit a referenced-message id. "
                "Supporting it requires an event-server change and a separate "
                "deploy. Use a Discord DM ref instead."
            )
        return LoginChannel(destination=raw, source=source, topic=f"{source}:{scope}")
    raise RuntimeError(
        f"{LOGIN_CHANNEL_ENV} source '{source}' is not supported for "
        "subscription login bootstrap."
    )


def _register_login_channel(project_path: Path, cfg: Config,
                            channel: LoginChannel) -> dict:
    """Ensure channel credentials/grants exist and return their live bubble.

    Channel registration is intentionally best-effort in the general event
    startup path: it returns an empty list for both invalid upstream credentials
    and a rejected bubble signature. Login bootstrap cannot treat those cases
    alike. When registration is empty, probe the bubble with a signed JOIN; only
    a :class:`BubbleRejected` permits a compare-and-swap re-mint and one retry.
    """
    from bobi.events.server import (
        BubbleRejected,
        deregister,
        ensure_bubble,
        register,
        register_discord_apps,
        register_slack_workspaces,
        register_whatsapp_numbers,
    )

    es_url = cfg.event_server_url
    if not es_url:
        raise RuntimeError(
            "event_server_url is not configured — cannot post the login URL "
            "through the channel gateway."
        )
    def register_channel(bubble: dict) -> list[str]:
        kwargs = {
            "bubble_id": bubble["bubble_id"],
            "bubble_key": bubble["bubble_key"],
        }
        if channel.source == "slack":
            return register_slack_workspaces(es_url, cfg, **kwargs)
        if channel.source == "discord":
            return register_discord_apps(es_url, cfg, **kwargs)
        if channel.source == "whatsapp":
            return register_whatsapp_numbers(es_url, cfg, **kwargs)
        return []

    bubble = ensure_bubble(es_url, project_path)
    registered = register_channel(bubble)
    if not registered:
        # A channel endpoint's 403 may mean either stale bubble auth or invalid
        # upstream channel credentials. A signed empty-subscription JOIN tests
        # only bubble membership, so a valid bubble never gets rotated merely
        # because Slack/Discord/WhatsApp configuration is wrong.
        try:
            probe_id, probe_key = register(
                es_url, "login-bootstrap-bubble-probe", [],
                bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
            )
        except BubbleRejected:
            bubble = ensure_bubble(
                es_url, project_path, force_remint_of=bubble["bubble_id"],
            )
            registered = register_channel(bubble)
        else:
            deregister(es_url, probe_id, probe_key)
    if not registered:
        raise RuntimeError(
            f"could not register {channel.source} credentials for "
            "subscription login bootstrap."
        )
    return bubble


def _post_login_message(project_path: Path, cfg: Config, channel: LoginChannel,
                        text: str, post_message) -> str:
    """Post into *channel* and return the posted message's id.

    The id is what the ask-first flow correlates replies against, and both
    paths already carry it: the legacy Slack path returns ``chat.postMessage``'s
    parsed body, and the gateway's ``channels_send`` documents ``ts`` as "the
    posted/updated message id". ``channel.thread_ts`` threads the post.
    """
    if channel.legacy_slack_channel:
        token = cfg.credential("slack", "bot_token")
        return _posted_message_id(
            post_message(token, channel.destination, text, channel.thread_ts)
        )
    _register_login_channel(project_path, cfg, channel)
    from bobi.events.gateway import channels_send
    return _posted_message_id(
        channels_send(project_path, _threaded_ref(channel), text, mode="post")
    )


def _posted_message_id(response: object) -> str:
    """The ``ts`` out of either post path's response, or "" when absent."""
    if isinstance(response, dict):
        return str(response.get("ts") or "")
    return ""


def _threaded_ref(channel: LoginChannel) -> str:
    """The gateway destination ref, anchored to ``channel.thread_ts``.

    Posting to a ``<destination>:thread:<ts>`` ref threads the message: the
    Slack channel adapter maps a parsed ``threadId`` straight onto ``threadTs``.
    """
    if not channel.thread_ts or ":thread:" in channel.destination:
        return channel.destination
    return f"{channel.destination}:thread:{channel.thread_ts}"


def _ensure_discord_inbound_ready(
    project_path: Path,
    cfg: Config,
    channel: LoginChannel,
    timeout: float = 10,
) -> None:
    """Fail before posting if the event server cannot receive Discord messages.

    Ask-first makes inbound Discord events a requirement of *every* flow, not
    just paste-back: a codex device login now waits for a human reply too. The
    old name described when it fired, so it changed with the call site.
    """
    _register_login_channel(project_path, cfg, channel)
    deadline = time.monotonic() + timeout
    app_id = channel.topic.removeprefix("discord:")
    last_health: dict | None = None
    from bobi.events.server import health

    while time.monotonic() < deadline:
        last_health = health(cfg.event_server_url)
        entries = (
            last_health.get("discord_gateway", [])
            if isinstance(last_health, dict) else []
        )
        entry = next(
            (
                e for e in entries
                if isinstance(e, dict) and str(e.get("application_id")) == app_id
            ),
            None,
        )
        if entry:
            state = str(entry.get("state") or "")
            if state == "connected":
                return
            if state == "fatal":
                reason = entry.get("fatal_reason") or "unknown fatal error"
                raise RuntimeError(
                    "Discord subscription login cannot receive "
                    f"Gateway events: {reason}."
                )
        time.sleep(0.5)

    mode = last_health.get("mode") if isinstance(last_health, dict) else None
    raise RuntimeError(
        "Discord subscription login requires an event server with "
        "the local Discord Gateway driver. The configured event server did not "
        f"report a connected Gateway for application {app_id}"
        + (f" (mode: {mode})." if mode else ".")
    )


def _conversation_matches(expected: str, actual: object, source: str) -> bool:
    if not expected:
        return True
    if actual == expected:
        return True
    # Slack inbound refs always include a thread anchor; allow a configured
    # base conversation ref to match the resulting thread conversation.
    return (
        source == "slack"
        and isinstance(actual, str)
        and len(expected.split(":")) == 4
        and actual.startswith(f"{expected}:thread:")
    )


def _is_channel_destination(channel: LoginChannel) -> bool:
    """True when the destination is a shared channel rather than a 1:1 DM.

    The one correlation rule (D5) is a property of the *destination*, not of
    the source: a channel sees unrelated traffic, so the reply's thread anchor
    is the only correlation available; a DM has nothing to correlate against,
    so the conversation is the correlation. The same predicate decides whether
    "reply in a thread" is the right thing to ask for.
    """
    return (
        channel.source in {"discord", "slack"}
        and ":channel:" in channel.destination
    ) or (
        channel.source == "slack" and bool(channel.legacy_slack_channel)
    )


def _reply_anchor(channel: LoginChannel, ask_ts: str) -> str:
    """The thread id a human reply to the ask will carry.

    ``$BOBI_LOGIN_CHANNEL`` may legitimately be a six-part threaded ref, and
    Slack has no nested threads: posting the ask to such a destination puts it
    *inside* that thread, so every reply carries the destination's own thread
    id and never the ask's ``ts``. An ask-``ts`` predicate would wait to
    timeout forever on a supported and tested configuration.
    """
    conv = parse_conversation(channel.destination)
    if conv is not None and conv.thread_id:
        return conv.thread_id
    return ask_ts


def _event_fields(event: dict) -> dict:
    raw = event.get("fields")
    return raw if isinstance(raw, dict) else {}


def _event_message_id(event: dict) -> str:
    """The inbound message's own id (``fields.ts``), or "" when absent."""
    return str(_event_fields(event).get("ts") or "")


def _event_is_human(event: dict) -> bool:
    """False for a bot-authored message.

    The Slack adapter sets ``fields.bot_id`` for any bot-authored message and
    already drops messages from *our own* bots, so this only adds third-party
    bots. Per Z5 there is no keyword and no allowlist beyond this.
    """
    return not _event_fields(event).get("bot_id")


def _event_text(event: dict) -> str:
    """The message text, read from all three shapes the adapters have used."""
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    return (event.get("text") or payload.get("text")
            or _event_fields(event).get("text") or "").strip()


def _event_for_destination(event: dict, channel: LoginChannel | str) -> bool:
    """True when the event came from the login destination.

    Source, conversation and channel-id filters, shared by the ready-reply and
    pasted-code predicates so there is one definition of "from the ask".
    """
    if isinstance(channel, str):
        expected_source, expected_conversation, expected_channel = "slack", "", channel
    else:
        expected_source = channel.source
        expected_conversation = (
            "" if channel.legacy_slack_channel else channel.destination
        )
        expected_channel = channel.legacy_slack_channel

    if (event.get("source") or "").lower() != expected_source:
        return False
    if not _conversation_matches(
        expected_conversation, event.get("conversation"), expected_source
    ):
        return False
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    fields = _event_fields(event)
    ev_channel = fields.get("channel") or payload.get("channel")
    # Filter to the login channel; a workspace subscription sees every channel.
    return not (expected_channel and ev_channel and ev_channel != expected_channel)


def _anchor_matches(event: dict, channel: LoginChannel, anchor: str) -> bool:
    """The one correlation rule (D5), keyed on the destination's shape.

    Read ``fields.thread_ts`` and *never* the event type: ``threadTs`` is
    parsed before any type branching and set for every emitted type, so a human
    who @mentions the bot inside the ask's thread arrives as ``slack.mention``
    carrying the anchor. Under Z5 that message is ready, so an event-type
    filter would reject a human reply the design must accept.
    """
    if not _is_channel_destination(channel):
        return True
    return bool(anchor) and str(_event_fields(event).get("thread_ts") or "") == anchor


def _reply_destination(event: dict, channel: LoginChannel,
                       anchor: str) -> LoginChannel:
    """Where every post after the ready reply goes: the ask's own thread."""
    if channel.legacy_slack_channel:
        return replace(channel, thread_ts=anchor)
    conversation = event.get("conversation")
    if isinstance(conversation, str) and conversation:
        # A Slack inbound ref is already thread-anchored.
        return replace(channel, destination=conversation, thread_ts="")
    return replace(channel, thread_ts=anchor)


def _ready_reply(event: dict, channel: LoginChannel, anchor: str,
                 consumed_ids: object = ()) -> LoginChannel | None:
    """Any *human* message correlated to the ask; the address to post into.

    Z5: no keyword and no allowlist - any human message counts as "ready".
    """
    if not _event_for_destination(event, channel):
        return None
    if not _event_is_human(event):
        return None
    if not _anchor_matches(event, channel, anchor):
        return None
    if not _event_text(event):
        return None
    message_id = _event_message_id(event)
    if message_id and message_id in consumed_ids:
        return None
    return _reply_destination(event, channel, anchor)


def _paste_back_instruction(channel: LoginChannel) -> str:
    if _is_channel_destination(channel):
        return (
            "Open this URL, authorize, then reply to this message with the "
            "code, or @mention the bot with the code:\n"
        )
    return (
        "Open this URL, authorize, then paste the code back "
        "*in this channel*:\n"
    )


def _extract_code(event: dict, channel: LoginChannel | str,
                  consumed_ids: object = ()) -> str | None:
    """Pull an auth code out of a chat event for the login destination.

    ``consumed_ids`` holds the message ids already spent satisfying an earlier
    phase. Rejecting them matters: this predicate accepts any non-empty text
    and returns its last whitespace-delimited token, so the ready reply that
    started the login would otherwise have its last word written into the pty
    as the OAuth code.
    """
    if not _event_for_destination(event, channel):
        return None
    text = _event_text(event)
    if not text:
        return None
    message_id = _event_message_id(event)
    if message_id and message_id in consumed_ids:
        return None
    # The human is told to paste only the code; tolerate a stray label/prefix
    # by taking the last whitespace-delimited token.
    return text.split()[-1]


@dataclass
class ChatListener:
    """A live chat listener: its client, its queue, and what it has consumed.

    One client and one queue serve every phase of a login, so a reply that
    arrives while an earlier phase is still running stays in the queue rather
    than being lost. ``consumed_ids`` is what keeps a *spent* reply from being
    read again by a later phase.
    """

    client: object
    queue: object
    channel: LoginChannel
    consumed_ids: set = field(default_factory=set)

    def stop(self) -> None:
        self.client.stop()


def _listener_channel(cfg: Config, channel: LoginChannel | str) -> LoginChannel:
    """Resolve *channel* and make sure a Slack destination carries its topic."""
    login_channel = (
        _resolve_login_channel(cfg, channel) if isinstance(channel, str) else channel
    )
    if login_channel.source == "slack" and not login_channel.topic:
        login_channel = replace(login_channel, topic=_slack_topic(cfg))
    return login_channel


def _connect_chat_listener(project_path: Path, channel: LoginChannel | str,
                           timeout: float) -> ChatListener:
    """Subscribe to the chat topic and block until the socket is live.

    Split from the wait so the ask can be posted *between* the two, which is
    what makes the ordering expressible rather than a comment. A reply lost in
    the gap between post and subscribe is unrecoverable, not merely late:
    replay is cursor-based per deployment, and each ``register`` mints a fresh
    deployment with a fresh session, so a deployment created after an event was
    published was never an indexed subscriber for it.
    """
    from queue import SimpleQueue

    from bobi.events.client import EventServerClient
    from bobi.events.server import BubbleRejected, ensure_bubble, register

    cfg = Config.load(project_path)
    es_url = cfg.event_server_url
    if not es_url:
        raise RuntimeError(
            "event_server_url is not configured — cannot receive the auth code."
        )
    login_channel = _listener_channel(cfg, channel)

    # Signed channel registration creates the resource grant required by the
    # global chat topic. It also returns the bubble it recovered to if the
    # server forgot the persisted one after a restart.
    bubble = _register_login_channel(project_path, cfg, login_channel)
    # A unique deployment name per run. Registering an existing name in the
    # same bubble deliberately removes the prior deployment and its
    # subscriptions, so two fixed-name runs in flight de-index each other: a
    # human reply to the first ask reaches only the second listener, whose
    # predicate rejects it, and the first command blocks to timeout having
    # posted a live ask nobody can satisfy.
    name = f"login-bootstrap-{os.getpid()}"
    try:
        deployment_id, api_key = register(
            es_url, name, [login_channel.topic],
            bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
        )
    except BubbleRejected:
        # The server can restart between channel registration and this JOIN.
        # Re-mint with the CAS guard, then recreate the bubble-scoped channel
        # credential/grant before retrying the listener exactly once.
        ensure_bubble(
            es_url, project_path, force_remint_of=bubble["bubble_id"],
        )
        bubble = _register_login_channel(project_path, cfg, login_channel)
        deployment_id, api_key = register(
            es_url, name, [login_channel.topic],
            bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
        )

    q: SimpleQueue = SimpleQueue()
    client = EventServerClient(es_url, deployment_id, api_key, queue=q)
    client.start()
    client.wait_connected(min(timeout, 30))
    return ChatListener(client=client, queue=q, channel=login_channel)


def _wait_for_chat_event(listener: ChatListener, timeout: float, match):
    """Consume the listener's queue until *match* returns a truthy value.

    The matched message's id is recorded as consumed, so a later phase sharing
    this queue cannot read the same message again. Returns None on timeout.
    """
    from queue import Empty

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = listener.queue.get(timeout=2)
        except Empty:
            continue
        matched = match(event)
        if matched:
            message_id = _event_message_id(event)
            if message_id:
                listener.consumed_ids.add(message_id)
            return matched
    return None


def _wait_for_code(project_path: Path, channel: LoginChannel | str,
                   timeout: float, listener: ChatListener | None = None) -> str:
    """Block for the pasted code, reusing the ask's listener when there is one."""
    own = listener is None
    if own:
        listener = _connect_chat_listener(project_path, channel, timeout)
    try:
        code = _wait_for_chat_event(
            listener, timeout,
            lambda event: _extract_code(
                event, listener.channel, listener.consumed_ids),
        )
    finally:
        if own:
            listener.stop()
    if code:
        return code
    raise TimeoutError(
        f"auth code not received over the event bus within {timeout:.0f}s"
    )


# --- the ask, and its one-per-kind state ------------------------------------

# A stable fragment of the ask, used to recognize this bot's own ask when the
# id has to be recovered from channel history.
_ASK_MARKER = "I will begin the login flow"


def _ask_state_path(kind: str) -> Path:
    """The ask's state file, one per *resolved* login kind.

    Named under codex's config dir - which the entrypoint's section 3c points
    at ``${DATA_DIR}/codex`` - so the entrypoint's single lstat refusal covers
    both the credential and this state. The kind is the resolved spec kind and
    never the raw argument: a codex-brained boot passes no target while the
    tool path passes ``codex``, and those are one credential, so one slot. On a
    claude brain the brain's ask and the codex tool's ask are two credentials
    and correctly two slots.
    """
    from bobi.brain.codex_config import codex_home

    return codex_home() / f".login-ask-{kind}"


def _read_ask_state(kind: str) -> dict:
    """The stored ask, or ``{}`` when there is none or it is unreadable."""
    try:
        data = json.loads(_ask_state_path(kind).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ask_state(kind: str, destination: str, message_id: str) -> None:
    """Record the ask: the destination it went to, and its message id.

    Written with an empty id immediately *before* the post and updated with the
    id immediately after. The post is a remote call and this write is a later
    local rename, and nothing makes the pair atomic, so a marker with no id is
    what tells the next boot that an ask may already exist.

    Every access is a whole-document read or a whole-document write, never a
    load-mutate-save, so the companion lock CLAUDE.md requires of
    read-modify-write state does not apply and is not taken.
    """
    from bobi.fsutil import atomic_write_text

    atomic_write_text(
        _ask_state_path(kind),
        json.dumps({"destination": destination, "ts": message_id}),
    )


def _clear_ask_state(kind: str) -> None:
    """Drop the stored ask. Called on success, and only on success."""
    _ask_state_path(kind).unlink(missing_ok=True)


def _slack_identity(cfg: Config) -> tuple[str, str]:
    """``(team_id, bot_user_id)`` for the Slack-only history reads."""
    from bobi.slack import require_app_identity

    team_id, _bot_id, bot_user_id, _app_id = require_app_identity(
        cfg.credential("slack", "bot_token")
    )
    return team_id, bot_user_id


def _history_ref(channel: LoginChannel, team_id: str, anchor: str = "") -> str:
    """The conversation ref to read history from.

    With *anchor* the ref is thread-anchored and reads that thread's replies;
    without it the ref is unanchored and reads the destination's own history.
    """
    if channel.legacy_slack_channel:
        base = f"slack:{team_id}:channel:{channel.legacy_slack_channel}"
        return f"{base}:thread:{anchor}" if anchor else base
    return _threaded_ref(replace(channel, thread_ts=anchor))


def _newest_human_message(messages: list, bot_user_id: str) -> dict | None:
    """The newest message after this bot's own last one, authored by a human.

    ``/channels/history`` returns messages oldest-first, so position is the
    ordering and no assumption about ``ts`` formats is needed. The baseline is
    the newest message this bot itself wrote: without it, a boot that already
    consumed the reply and then timed out mid-login would re-consume it on
    every restart and re-post a device code each time, which is the Slack noise
    D1 exists to prevent.
    """
    last_bot = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and str(message.get("user") or "") == bot_user_id:
            last_bot = index
    for message in reversed(messages[last_bot + 1:]):
        if not isinstance(message, dict):
            continue
        user = str(message.get("user") or "")
        # An empty `user` is not treated as human, so the rule fails closed.
        # The history path has no authorship field at all, so a non-empty user
        # is the strongest signal available (D9 accepts that bound).
        if not user or user == bot_user_id:
            continue
        if not str(message.get("text") or "").strip():
            continue
        return message
    return None


def _skip_history(channel: LoginChannel, what: str) -> bool:
    """True when *channel*'s transport has no usable history for *what*.

    Slack-only, and that is a property of the adapters rather than a choice:
    WhatsApp has no history at all (its channel adapter implements send and
    uploadFiles only, and ``/channels/history`` rejects an adapter with no
    ``fetchConversation`` outright), and Discord has history but no usable bot
    identity (its conversation messages carry ``author.username``, so the
    "newest message authored by this bot" baseline cannot be established).

    The degradation is bounded and not a lost credential: the ask still exists,
    the next boot still re-attaches and still blocks on the live listener, so a
    reply that landed in the restart gap has to be sent again.
    """
    if channel.source == "slack":
        return False
    log.info(
        "%s skipped: %s has no usable conversation history; blocking on the "
        "live listener instead.", what, channel.source,
    )
    return True


def _catch_up_read(project_path: Path, cfg: Config, channel: LoginChannel,
                   anchor: str) -> tuple[LoginChannel, str] | None:
    """One bounded read of the ask's thread, so a reply in the restart gap is
    not lost. Returns ``(where to reply, the reply's message id)`` or None.

    This is the one real defect "do not re-post" introduces that re-posting did
    not have: a human who answers between the timeout exit and the next
    listener's connect gets no response and no second ask. Replay cannot cover
    it, because each ``register`` mints a fresh deployment with an empty buffer.
    """
    if _skip_history(channel, "Catch-up read"):
        return None
    from bobi.events.gateway import channels_history

    team_id, bot_user_id = _slack_identity(cfg)
    messages = channels_history(project_path, _history_ref(channel, team_id, anchor))
    message = _newest_human_message(messages, bot_user_id)
    if message is None:
        return None
    log.info("Catch-up read found a reply that landed while nothing was listening.")
    return replace(channel, thread_ts=anchor), str(message.get("ts") or "")


def _recover_ask_id(project_path: Path, cfg: Config,
                    channel: LoginChannel) -> str:
    """Recover a posted ask's id from the destination's own history.

    Covers the one window "one ask ever" cannot close: a death between a post
    the platform accepted and the local write that records its id would leave a
    visible ask with no stored id, and the next boot would post a second one.
    An unanchored ref reads channel history rather than thread replies. A real
    duplicate then requires the post to have failed *and* this read to miss it.
    """
    if _skip_history(channel, "Ask-id recovery"):
        return ""
    from bobi.events.gateway import channels_history

    team_id, bot_user_id = _slack_identity(cfg)
    messages = channels_history(project_path, _history_ref(channel, team_id))
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("user") or "") != bot_user_id:
            continue
        if _ASK_MARKER in str(message.get("text") or ""):
            return str(message.get("ts") or "")
    return ""


def _ask_text(project_path: Path, channel: LoginChannel,
              spec: SubscriptionLogin) -> str:
    """The ask. Z4's wording, naming the agent, the instance and the target so
    two machines in one fleet do not post indistinguishable asks.

    It asks for a thread only when the destination is a channel: "reply in a
    thread" is wrong on a threadless DM, and the same ``is_channel`` predicate
    decides it as decides the correlation rule.
    """
    from bobi.identity import resolve_deployment_identity

    identity = resolve_deployment_identity(project_path)
    where = str(identity.get("instance") or "unknown")
    machine = identity.get("machine")
    if machine:
        where = f"{where} ({machine})"
    how = (
        "Reply to this message in a thread"
        if _is_channel_destination(channel) else
        "Reply in this channel"
    )
    return (
        f"🔐 *bobi subscription login* - `{spec.kind}` on {where}\n"
        f"{how} when you are ready, and then {_ASK_MARKER}."
    )


def _establish_ready_reply(
    project_path: Path, cfg: Config, channel: LoginChannel,
    spec: SubscriptionLogin, listener: ChatListener, ask_timeout: float,
    post_message,
) -> tuple[LoginChannel, LoginChannel | None]:
    """Re-attach or ask, then block for a human reply (Z2, Z4, D1).

    Exactly one of three happens: a stored ask with an id does one catch-up
    read and posts nothing; a stored marker with no id recovers the id from the
    destination's history and posts nothing; no stored state writes the marker,
    posts the ask, then records the posted message's id.

    Returns ``(the ask's thread, where the human replied)``; the second is None
    when nobody replied inside *ask_timeout*.
    """
    state = _read_ask_state(spec.kind)
    stored = str(state.get("destination") or "")
    reattached = bool(state) and stored == channel.destination
    ask_ts = str(state.get("ts") or "") if reattached else ""

    if reattached and not ask_ts:
        ask_ts = _recover_ask_id(project_path, cfg, channel)
        if ask_ts:
            _write_ask_state(spec.kind, channel.destination, ask_ts)

    if reattached:
        log.info(
            "Re-attaching to the existing login ask (%s); posting nothing.",
            ask_ts or "id not recovered",
        )
        anchor = _reply_anchor(channel, ask_ts)
        if ask_ts:
            caught_up = _catch_up_read(project_path, cfg, channel, anchor)
            if caught_up is not None:
                reply_to, message_id = caught_up
                if message_id:
                    listener.consumed_ids.add(message_id)
                return replace(channel, thread_ts=anchor), reply_to
    else:
        if state:
            log.info(
                "The stored ask went to %s but the resolved destination is %s; "
                "treating it as absent and posting a fresh ask.",
                stored or "(nowhere)", channel.destination,
            )
        _write_ask_state(spec.kind, channel.destination, "")
        ask_ts = _post_login_message(
            project_path, cfg, channel,
            _ask_text(project_path, channel, spec), post_message,
        )
        _write_ask_state(spec.kind, channel.destination, ask_ts)
        anchor = _reply_anchor(channel, ask_ts)

    reply_to = _wait_for_chat_event(
        listener, ask_timeout,
        lambda event: _ready_reply(event, channel, anchor, listener.consumed_ids),
    )
    return replace(channel, thread_ts=anchor), reply_to


def _quarantine_credential(home: Path) -> Path | None:
    """Move an existing credential aside before a rebind, collision-free.

    Codex validation requires only a non-blank refresh token; it never proves
    the token is live or bound to the intended account. So a revoked,
    server-invalidated or wrong-account file is structurally valid and would
    short-circuit the login in exactly the cases that produce a real 401.

    Refuses anything that is not a regular file. The reason is correctness, not
    privilege: this runs as the ``bobi`` user, and renaming a symlink would
    move the link while leaving the real credential live, so the quarantine
    would silently not happen.
    """
    path = credentials_path(home)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(
            f"refusing to rebind: {path} is not a regular file, so moving it "
            "aside would leave the real credential in place."
        )
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = path.with_name(f"{path.name}.quarantined-{stamp}")
    collision = 1
    while target.exists():
        target = path.with_name(f"{path.name}.quarantined-{stamp}-{collision}")
        collision += 1
    path.rename(target)
    log.info("Quarantined the existing credential to %s before rebinding.", target)
    return target


def _reap_login(proc, master_fd: int) -> None:
    """Close the pty and make sure the login process is really gone.

    The child is spawned ``start_new_session=True`` and is therefore its own
    session leader, so a login CLI that ignores SIGTERM survives as an orphan
    holding the pty. Signal the process *group*, wait with a bound, escalate to
    SIGKILL, and reap.
    """
    import signal

    try:
        os.close(master_fd)
    except OSError:
        pass
    if proc.poll() is not None:
        return
    for sig, grace, fallback in (
        (signal.SIGTERM, 10, "terminate"), (signal.SIGKILL, 5, "kill"),
    ):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:  # noqa: BLE001 - no pgid (or a test double); signal directly
            try:
                getattr(proc, fallback)()
            except Exception:  # noqa: BLE001 - already gone
                return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    log.warning(
        "login process %s survived SIGKILL; leaving it to the kernel.",
        getattr(proc, "pid", "?"),
    )


# --- orchestration ----------------------------------------------------------

def run_bootstrap(
    project_path: Path,
    *,
    target: str | None = None,
    rebind: bool = False,
    timeout: float = 600,
    url_timeout: float = 120,
    ask_timeout: float = 1800,
    device_timeout: float = 900,
    spawn_login=None,
    post_message=None,
    wait_for_code=None,
    scrape_login=None,
    connect_listener=None,
) -> bool:
    """Drive the full subscription login, ask-first. True if credentials landed.

    *target* names a login spec (``claude`` or ``codex``) and defaults to the
    team's brain, so one primitive serves both triggers: the brain at boot and
    a CLI tool on demand (Z3). *rebind* skips the credential short-circuit and
    quarantines the existing file once a human replies, so a revoked or
    wrong-account credential heals without ``fly ssh`` (D7).

    Nothing credential-granting is posted until a human says they are ready
    (Z2, Z4): the listener is registered, the ask is posted, and only the reply
    starts the login CLI. The pty spawn, chat post, listener, code scrape and
    code wait are injectable so the orchestration is unit-testable without a
    real CLI, Slack, or Worker.

    Four phases get four budgets, because a single one spanning the human wait
    and the device authorization makes a human who authorizes at minute eleven
    lose on a code that is valid for fifteen: *ask_timeout* for the ready
    reply, *url_timeout* for the scrape, *timeout* for a pasted code, and
    *device_timeout* for the device authorization.

    The destination is ``$BOBI_LOGIN_CHANNEL`` and takes no override - see the
    comment on ``channel_ref`` below.
    """
    spawn_login = spawn_login or _spawn_login
    post_message = post_message or post_slack_message
    wait_for_code = wait_for_code or _wait_for_code
    scrape_login = scrape_login or _scrape_login
    connect_listener = connect_listener or _connect_chat_listener

    # Resolve the team's brain so credential path, login command, and flow are
    # all the right ones. Loading cfg here also seeds BOBI_BRAIN for the
    # spec lookups below (and the spawned login subprocess).
    cfg = Config.load(project_path)

    # Validate the target against the login specs, not the brain registry.
    # `_active_spec` falls back to Claude for any unrecognized value, so an
    # unvalidated target would silently run the *Claude* flow - a guard bypass
    # rather than a closed input. The brain registry is the wrong allow-list:
    # it carries `stub`, and it misses the two aliases the rest of the tree
    # honours (`gateway` -> claude, `gateway-openai` -> codex).
    resolved_kind = normalize_brain_kind(target) if target else None
    if target and resolved_kind not in _SPECS:
        raise RuntimeError(
            f"login target '{target}' is not one of: {', '.join(sorted(_SPECS))}"
        )
    # Both brain-credential refusals below are arguments about *Claude's
    # credential path*, not about whether an argument was supplied. Scoping
    # them by the resolved provider newly permits exactly one case:
    # `login-bootstrap codex` on a non-codex brain, which is what #958 is about.
    claude_credential = resolved_kind in (None, "claude")

    configured_engine = normalize_brain_kind(cfg.brain_kind) or DEFAULT_BRAIN
    if cfg.brain_is_gateway and claude_credential and (
        configured_engine != "claude"
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    ):
        # A Claude gateway can deliberately use subscription OAuth when the
        # endpoint accepts it. Other gateways, or a configured gateway token,
        # must keep using the gateway-specific credential path.
        raise RuntimeError(
            "subscription login applies only to a Claude gateway without "
            "ANTHROPIC_AUTH_TOKEN; this gateway authenticates with gateway "
            "credentials in the runtime .env."
        )
    set_process_brain_from_config(cfg)
    if resolved_kind:
        # One assignment retargets the whole flow: every downstream helper
        # resolves its spec from this env var, so `login_cmd`, `flow`,
        # `shadow_env` and `credentials_path` all follow. It also leaves the
        # `spawn_login(home)` call site byte-identical, so no injected fake
        # breaks - `_spawn_login` re-reads the spec internally.
        os.environ[BRAIN_ENV] = resolved_kind
    spec = _active_spec()

    home = Path(os.environ.get("HOME", str(Path.home())))
    if credentials_exist(home) and not rebind:
        log.info("Credentials already present at %s — skipping bootstrap.",
                 credentials_path(home))
        return True

    if claude_credential and os.environ.get(spec.shadow_env):
        raise RuntimeError(
            f"{spec.shadow_env} is set; it overrides subscription auth. "
            "Unset it before subscription login."
        )

    # The destination is configuration, never a caller's argument. The login
    # URL is a live credential-granting link, and the code pasted back into it
    # is read from this same channel and written to the login CLI's stdin - so
    # whoever picks the channel drives both ends of the flow. `login-bootstrap`
    # sits on the `agent` group any worker's shell can reach, and a worker
    # handling a fork diff, an issue body, or a webhook payload is handling
    # untrusted text (docs/SECURITY.md). It once took a `--channel` that
    # outranked this env var; it does not.
    channel_ref = os.environ.get(LOGIN_CHANNEL_ENV, "")
    if not channel_ref:
        raise RuntimeError(
            f"{LOGIN_CHANNEL_ENV} is unset — need a private chat channel to post "
            "the login URL into."
        )
    login_channel = _resolve_login_channel(cfg, channel_ref)
    if login_channel.source == "discord":
        # Ask-first means every flow now needs inbound Discord events, not
        # just paste-back: a codex device login waits for a human reply too.
        _ensure_discord_inbound_ready(project_path, cfg, login_channel)

    login_cmd_str = " ".join(spec.login_cmd)
    # The listener is registered before the ask is posted, and that order is
    # load-bearing: a reply lost in the gap between post and subscribe is
    # unrecoverable, not merely late.
    listener = connect_listener(project_path, login_channel, ask_timeout)
    try:
        thread, reply_to = _establish_ready_reply(
            project_path, cfg, login_channel, spec, listener, ask_timeout,
            post_message,
        )
        if reply_to is None:
            # Say so in the thread rather than ending on "waiting for you",
            # so a late reply has somewhere to read what happened. The stored
            # ask id is deliberately left in place: the next boot re-attaches
            # to this same thread instead of posting a second ask (D1).
            _post_message_best_effort(
                project_path, cfg, thread,
                "⌛ *bobi subscription login* - nobody replied within "
                f"{ask_timeout / 60:.0f} minutes, so this attempt stopped. "
                "Reply here again and I will start the login flow.",
                post_message,
            )
            raise TimeoutError(
                "no human replied to the subscription-login ask within "
                f"{ask_timeout:.0f}s"
            )

        quarantined = _quarantine_credential(home) if rebind else None

        proc, master = spawn_login(home)
        try:
            if spec.flow == "paste_back":
                # Claude: scrape the URL, post it, wait for the human to paste
                # the code back over chat, write it into the pty.
                url = _read_until_url(master, url_timeout)
                log.info(
                    "Captured login URL; posting to %s login channel %s.",
                    login_channel.source, login_channel.destination,
                )
                _post_login_message(
                    project_path, cfg, reply_to,
                    "🔐 *bobi subscription login*\n"
                    + _paste_back_instruction(login_channel)
                    + url,
                    post_message,
                )
                code = wait_for_code(
                    project_path, reply_to, timeout, listener=listener,
                )
                _write_line(master, code)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    log.warning("login did not exit within 60s after the code.")
            else:
                # Codex device-poll: scrape the URL **and** the one-time code,
                # post both, then just wait - the CLI polls until the human
                # authorizes; nothing is pasted back. Authorization finishing
                # and the CLI exiting are the same event, so `proc.wait` is the
                # only signal this flow has.
                url, code = scrape_login(master, url_timeout, spec)
                log.info(
                    "Captured device URL + code; posting to %s login channel %s.",
                    login_channel.source, login_channel.destination,
                )
                _post_login_message(
                    project_path, cfg, reply_to,
                    "🔐 *bobi subscription login*\n"
                    "Open this link, sign in, then enter the one-time code:\n"
                    f"{url}\n"
                    f"Code: `{code}`\n"
                    "_Waiting for you to authorize…_",
                    post_message,
                )
                try:
                    proc.wait(timeout=device_timeout)
                except subprocess.TimeoutExpired:
                    log.warning("device login was not authorized within %.0fs.",
                                device_timeout)
        finally:
            _reap_login(proc, master)

        ok = credentials_exist(home)
        result_msg = (
            "✅ Subscription login complete — starting up."
            if ok else
            "❌ Login failed — no credentials were written. Fallback: "
            f"`fly ssh console` then `{login_cmd_str}`."
        )
        if quarantined is not None:
            result_msg += f"\nPrevious credential kept at `{quarantined}`."
        _post_message_best_effort(
            project_path, cfg, reply_to, result_msg, post_message)
        if ok:
            # Cleared on success and only on success: an implementation that
            # cleared it on a failed post would post a second ask next boot.
            _clear_ask_state(spec.kind)
        return ok
    finally:
        listener.stop()


def _post_message_best_effort(project_path: Path, cfg: Config,
                              channel: LoginChannel, text: str,
                              post_message) -> None:
    """Post an outcome. A chat hiccup must not change the login's result."""
    try:
        _post_login_message(project_path, cfg, channel, text, post_message)
    except Exception as exc:  # noqa: BLE001 — best-effort status post
        log.warning("Could not post bootstrap result to chat channel: %s", exc)


def _credential_status_cli(argv: list[str] | None = None) -> int:
    """Machine-facing local check used by the container entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=["credential-status"])
    parser.add_argument("kind", choices=sorted(_SPECS))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    status = subscription_credentials_status(args.path, args.kind)
    state = "valid" if status.valid else "invalid"
    print(f"credentials {state}: {status.reason}")
    return 0 if status.valid else 1


if __name__ == "__main__":
    raise SystemExit(_credential_status_cli())
