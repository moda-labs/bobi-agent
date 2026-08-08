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
from dataclasses import dataclass
from pathlib import Path

from bobi.brain import BRAIN_ENV, set_process_brain
from bobi.config import Config
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
    creds_relpath: tuple[str, ...]   # OAuth credential file, relative to $HOME
    shadow_env: str                  # the API key that would silently outrank subscription auth
    flow: str                        # "paste_back" (claude) | "device_poll" (codex)
    url_re: re.Pattern               # scrape the sign-in URL from pty output
    code_re: re.Pattern | None = None  # device_poll: also scrape the one-time code


@dataclass(frozen=True)
class CredentialStatus:
    """Local structural validity of a subscription credential file."""

    valid: bool
    reason: str


@dataclass(frozen=True)
class LoginChannel:
    """Destination and subscription details for the bootstrap chat channel."""

    destination: str
    source: str
    topic: str
    legacy_slack_channel: str = ""


_SPECS: dict[str, SubscriptionLogin] = {
    "claude": SubscriptionLogin(
        kind="claude",
        login_cmd=("claude", "auth", "login", "--claudeai"),
        creds_relpath=(".claude", ".credentials.json"),
        shadow_env="ANTHROPIC_API_KEY",
        flow="paste_back",
        # e.g. https://claude.com/cai/oauth/authorize?code=true&client_id=...
        url_re=re.compile(r"https://\S+/oauth/authorize\S+"),
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
    """Path to the active brain's subscription OAuth credentials on the volume."""
    base = home or Path(os.environ.get("HOME", str(Path.home())))
    return Path(base, *_active_spec().creds_relpath)


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

def _parse_conversation(ref: str) -> tuple[str, str, str, str] | None:
    """Return ``(source, scope, chat_type, chat_id)`` for a gateway ref."""
    parts = ref.split(":")
    if len(parts) not in (4, 6):
        return None
    if any(not p for p in parts):
        return None
    if len(parts) == 6 and parts[4] != "thread":
        return None
    source, scope, chat_type, chat_id = parts[:4]
    if chat_type not in {"dm", "group", "channel"}:
        return None
    return source, scope, chat_type, chat_id


def _slack_topic(cfg: Config) -> str:
    from bobi.slack import resolve_app_id, resolve_auth_info

    token = cfg.credential("slack", "bot_token")
    team_id, bot_id, _ = resolve_auth_info(token)
    if not team_id:
        raise RuntimeError("could not resolve Slack team_id from bot_token.")
    app_id = resolve_app_id(token, bot_id)
    return f"slack:{team_id}:app:{app_id}" if app_id else f"slack:{team_id}"


def _resolve_login_channel(cfg: Config, raw: str) -> LoginChannel:
    """Resolve ``BOBI_LOGIN_CHANNEL`` to a post destination and bus topic."""
    conv = _parse_conversation(raw)
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

    source, scope, _chat_type, _chat_id = conv
    if source == "slack":
        return LoginChannel(destination=raw, source=source, topic=_slack_topic(cfg))
    if source in {"discord", "whatsapp"}:
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
                        text: str, post_message) -> None:
    if channel.legacy_slack_channel:
        token = cfg.credential("slack", "bot_token")
        post_message(token, channel.destination, text)
        return
    _register_login_channel(project_path, cfg, channel)
    from bobi.events.gateway import channels_send
    channels_send(project_path, channel.destination, text, mode="post")


def _ensure_discord_paste_back_ready(
    project_path: Path,
    cfg: Config,
    channel: LoginChannel,
    timeout: float = 10,
) -> None:
    """Fail before posting if the event server cannot receive Discord messages."""
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
                    "Discord subscription-login paste-back cannot receive "
                    f"Gateway events: {reason}."
                )
        time.sleep(0.5)

    mode = last_health.get("mode") if isinstance(last_health, dict) else None
    raise RuntimeError(
        "Discord subscription-login paste-back requires an event server with "
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


def _paste_back_instruction(channel: LoginChannel) -> str:
    if channel.source == "discord" and ":channel:" in channel.destination:
        return (
            "Open this URL, authorize, then reply to this message with the "
            "code, or @mention the bot with the code:\n"
        )
    return (
        "Open this URL, authorize, then paste the code back "
        "*in this channel*:\n"
    )


def _extract_code(event: dict, channel: LoginChannel | str) -> str | None:
    """Pull an auth code out of a chat event for the login destination."""
    if isinstance(channel, str):
        expected_source = "slack"
        expected_conversation = ""
        expected_channel = channel
    else:
        expected_source = channel.source
        expected_conversation = (
            "" if channel.legacy_slack_channel else channel.destination
        )
        expected_channel = channel.legacy_slack_channel

    if (event.get("source") or "").lower() != expected_source:
        return None
    if not _conversation_matches(
        expected_conversation, event.get("conversation"), expected_source
    ):
        return None
    raw_fields = event.get("fields")
    fields = raw_fields if isinstance(raw_fields, dict) else {}
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    ev_channel = fields.get("channel") or payload.get("channel")
    # Filter to the login channel; a workspace subscription sees every channel.
    if expected_channel and ev_channel and ev_channel != expected_channel:
        return None
    # The real Slack adapter (event-server/core/src/adapters/chat-sdk-slack.ts) puts the
    # message text at the event TOP LEVEL and in `payload.text`; `fields` carries only
    # channel/channel_type/user_id/ts. Read all three so we match the live shape
    # (top-level first) while staying tolerant of older event variants.
    text = (event.get("text") or payload.get("text")
            or fields.get("text") or "").strip()
    if not text:
        return None
    # The human is told to paste only the code; tolerate a stray label/prefix
    # by taking the last whitespace-delimited token.
    return text.split()[-1]


def _wait_for_code(project_path: Path, channel: LoginChannel | str,
                   timeout: float) -> str:
    """Subscribe to the chat topic and block for the pasted code."""
    from queue import Empty, SimpleQueue

    from bobi.events.client import EventServerClient
    from bobi.events.server import BubbleRejected, ensure_bubble, register

    cfg = Config.load(project_path)
    es_url = cfg.event_server_url
    if not es_url:
        raise RuntimeError(
            "event_server_url is not configured — cannot receive the auth code."
        )
    login_channel = (
        _resolve_login_channel(cfg, channel) if isinstance(channel, str) else channel
    )
    if login_channel.source == "slack" and not login_channel.topic:
        login_channel = LoginChannel(
            destination=login_channel.destination,
            source="slack",
            topic=_slack_topic(cfg),
            legacy_slack_channel=login_channel.legacy_slack_channel,
        )

    # Signed channel registration creates the resource grant required by the
    # global chat topic. It also returns the bubble it recovered to if the
    # server forgot the persisted one after a restart.
    bubble = _register_login_channel(project_path, cfg, login_channel)
    try:
        deployment_id, api_key = register(
            es_url, "login-bootstrap", [login_channel.topic],
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
            es_url, "login-bootstrap", [login_channel.topic],
            bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
        )

    q: SimpleQueue = SimpleQueue()
    client = EventServerClient(es_url, deployment_id, api_key, queue=q)
    client.start()
    client.wait_connected(min(timeout, 30))

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                event = q.get(timeout=2)
            except Empty:
                continue
            code = _extract_code(event, login_channel)
            if code:
                return code
    finally:
        client.stop()
    raise TimeoutError(
        f"auth code not received over the event bus within {timeout:.0f}s"
    )


# --- orchestration ----------------------------------------------------------

def run_bootstrap(
    project_path: Path,
    *,
    channel: str | None = None,
    timeout: float = 600,
    url_timeout: float = 120,
    spawn_login=None,
    post_message=None,
    wait_for_code=None,
    scrape_login=None,
) -> bool:
    """Drive the full subscription login. Returns True if credentials landed.

    The brain is resolved from ``agent.yaml`` ``brain.kind`` (so the right login
    CLID/flow/credential path is used). The pty spawn, Slack post, code scrape,
    and event-bus wait are injectable so the orchestration is unit-testable
    without a real CLI, Slack, or Worker.
    """
    spawn_login = spawn_login or _spawn_login
    post_message = post_message or post_slack_message
    wait_for_code = wait_for_code or _wait_for_code
    scrape_login = scrape_login or _scrape_login

    # Resolve the team's brain so credential path, login command, and flow are
    # all the right ones. Loading cfg here also seeds BOBI_BRAIN for the
    # spec lookups below (and the spawned login subprocess).
    cfg = Config.load(project_path)
    if cfg.brain_is_gateway:
        # No spec fallback: _active_spec would silently drive the CLAUDE
        # subscription login for a team that authenticates with gateway-specific
        # credentials (or nothing) - mirror the DeployError (#655/#777).
        raise RuntimeError(
            "subscription login does not apply to a gateway team - a gateway "
            "authenticates with gateway credentials in the runtime .env."
        )
    set_process_brain(cfg.brain_kind)
    spec = _active_spec()

    home = Path(os.environ.get("HOME", str(Path.home())))
    if credentials_exist(home):
        log.info("Credentials already present at %s — skipping bootstrap.",
                 credentials_path(home))
        return True

    if os.environ.get(spec.shadow_env):
        raise RuntimeError(
            f"{spec.shadow_env} is set; it overrides subscription auth. "
            "Unset it before subscription login."
        )

    channel_ref = channel or os.environ.get(LOGIN_CHANNEL_ENV, "")
    if not channel_ref:
        raise RuntimeError(
            f"{LOGIN_CHANNEL_ENV} is unset — need a private chat channel to post "
            "the login URL into."
        )
    login_channel = _resolve_login_channel(cfg, channel_ref)
    if spec.flow == "paste_back" and login_channel.source == "discord":
        _ensure_discord_paste_back_ready(project_path, cfg, login_channel)

    login_cmd_str = " ".join(spec.login_cmd)
    proc, master = spawn_login(home)
    try:
        if spec.flow == "paste_back":
            # Claude: scrape the URL, post it, wait for the human to paste the
            # code back over Slack, write it into the pty.
            url = _read_until_url(master, url_timeout)
            log.info(
                "Captured login URL; posting to %s login channel %s.",
                login_channel.source, login_channel.destination,
            )
            _post_login_message(
                project_path, cfg, login_channel,
                "🔐 *bobi subscription login*\n"
                + _paste_back_instruction(login_channel)
                + url,
                post_message,
            )
            code = wait_for_code(project_path, login_channel, timeout)
            _write_line(master, code)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                log.warning("login did not exit within 60s after the code.")
        else:
            # Codex device-poll: scrape the URL **and** the one-time code, post
            # both, then just wait — the CLI polls until the human authorizes;
            # nothing is pasted back.
            url, code = scrape_login(master, url_timeout, spec)
            log.info(
                "Captured device URL + code; posting to %s login channel %s.",
                login_channel.source, login_channel.destination,
            )
            _post_login_message(
                project_path, cfg, login_channel,
                "🔐 *bobi subscription login*\n"
                "Open this link, sign in, then enter the one-time code:\n"
                f"{url}\n"
                f"Code: `{code}`\n"
                "_Waiting for you to authorize…_",
                post_message,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("device login was not authorized within %.0fs.",
                            timeout)
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()

    ok = credentials_exist(home)
    result_msg = (
        "✅ Subscription login complete — starting up."
        if ok else
        "❌ Login failed — no credentials were written. Fallback: "
        f"`fly ssh console` then `{login_cmd_str}`."
    )
    try:
        _post_login_message(project_path, cfg, login_channel, result_msg, post_message)
    except Exception as exc:  # noqa: BLE001 — best-effort status post
        log.warning("Could not post bootstrap result to chat channel: %s", exc)
    return ok


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
