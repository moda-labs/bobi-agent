"""Subscription-login bootstrap (containerized-23 / #343).

At container first boot in subscription auth mode with no credentials on the
volume, drive ``claude auth login --claudeai`` under a pty: scrape the OAuth
URL, post it to a private Slack channel via the bot token already in env, then
wait for the human to paste the auth code back — which arrives as a Slack
message event over the event bus — and write it into the pty. Refresh-token
rotation makes this a once-per-machine ceremony (CONTAINERIZED_INSTANCES.md
§6.1); the manual fallback is ``fly ssh console`` + ``claude auth login``.

The live round-trip needs a real Worker + Slack and is exercised in the
deployed environment (alongside C10/C12). The mechanism here is unit-tested
with the pty, the Slack post, and the event source faked — see
tests/test_auth_bootstrap.py.
"""
from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import time
from pathlib import Path

from modastack.config import Config
from modastack.slack import post_slack_message

log = logging.getLogger(__name__)

# The login URL claude prints, e.g.
# https://claude.com/cai/oauth/authorize?code=true&client_id=...
_URL_RE = re.compile(r"https://\S+/oauth/authorize\S+")

# Env var naming the private Slack channel to post the login URL into. A
# private channel is a hard requirement (§6.1, C23): the code is single-use but
# grants the login to whoever pastes it first.
LOGIN_CHANNEL_ENV = "MODASTACK_LOGIN_CHANNEL"


def credentials_path(home: Path | None = None) -> Path:
    """Path to the Claude subscription OAuth credentials on the volume."""
    base = home or Path(os.environ.get("HOME", str(Path.home())))
    return Path(base) / ".claude" / ".credentials.json"


def credentials_exist(home: Path | None = None) -> bool:
    return credentials_path(home).is_file()


def needs_bootstrap(home: Path | None = None) -> bool:
    """True iff we're in subscription mode with no credentials yet."""
    if os.environ.get("MODASTACK_AUTH", "api_key") != "subscription":
        return False
    return not credentials_exist(home)


# --- pty driver -------------------------------------------------------------

def _spawn_login(home: Path) -> tuple[subprocess.Popen, int]:
    """Spawn ``claude auth login --claudeai`` on a pty. Returns (proc, master_fd)."""
    import pty

    master, slave = pty.openpty()
    env = dict(os.environ)
    env["HOME"] = str(home)
    # ANTHROPIC_API_KEY silently outranks subscription creds (§6.1) — never let
    # it leak into the login subprocess.
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        ["claude", "auth", "login", "--claudeai"],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    return proc, master


def _read_until_url(master_fd: int, timeout: float) -> str:
    """Read pty output until the OAuth URL appears; return it."""
    deadline = time.monotonic() + timeout
    buf = ""
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
        match = _URL_RE.search(buf)
        if match:
            return match.group(0)
    raise TimeoutError(f"did not see the claude login URL within {timeout:.0f}s")


def _write_line(master_fd: int, text: str) -> None:
    os.write(master_fd, (text.strip() + "\n").encode())


# --- event-bus wait ---------------------------------------------------------

def _extract_code(event: dict, channel: str) -> str | None:
    """Pull an auth code out of a Slack message event for ``channel``."""
    if (event.get("source") or "").lower() != "slack":
        return None
    fields = event.get("fields") if isinstance(event.get("fields"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    ev_channel = fields.get("channel") or payload.get("channel")
    # Filter to the login channel; a workspace subscription sees every channel.
    if channel and ev_channel and ev_channel != channel:
        return None
    # The real Slack adapter (event-server/src/adapters/slack.ts) puts the message
    # text at the event TOP LEVEL and in `payload.text`; `fields` carries only
    # channel/channel_type/user_id/ts. Read all three so we match the live shape
    # (top-level first) while staying tolerant of older event variants.
    text = (event.get("text") or payload.get("text")
            or fields.get("text") or "").strip()
    if not text:
        return None
    # The human is told to paste only the code; tolerate a stray label/prefix
    # by taking the last whitespace-delimited token.
    return text.split()[-1]


def _wait_for_code(project_path: Path, channel: str, timeout: float) -> str:
    """Subscribe to the workspace Slack topic and block for the pasted code."""
    from queue import Empty, SimpleQueue

    from modastack.events.client import EventServerClient
    from modastack.events.server import (
        _slack_auth_info,
        ensure_bubble,
        register,
        register_slack_workspaces,
    )

    cfg = Config.load(project_path)
    es_url = cfg.event_server_url
    if not es_url:
        raise RuntimeError(
            "event_server_url is not configured — cannot receive the auth code."
        )
    token = cfg.credential("slack", "bot_token")

    # Resolve the bubble first so the Slack registration can be signed — a signed
    # registration also creates the bubble-scoped record outbound send needs.
    bubble = ensure_bubble(es_url, project_path)

    # Ensure the Worker holds the bot token so it ingests this channel's messages.
    register_slack_workspaces(
        es_url, cfg,
        bubble_id=bubble["bubble_id"], bubble_key=bubble["bubble_key"],
    )
    team_id, _ = _slack_auth_info(token)
    if not team_id:
        raise RuntimeError("could not resolve Slack team_id from bot_token.")
    topic = f"slack:{team_id}"

    deployment_id, api_key = register(
        es_url, "login-bootstrap", [topic],
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
            code = _extract_code(event, channel)
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
) -> bool:
    """Drive the full subscription login. Returns True if credentials landed.

    The pty spawn, Slack post, and event-bus wait are injectable so the
    orchestration is unit-testable without a real claude binary, Slack, or
    Worker.
    """
    spawn_login = spawn_login or _spawn_login
    post_message = post_message or post_slack_message
    wait_for_code = wait_for_code or _wait_for_code

    home = Path(os.environ.get("HOME", str(Path.home())))
    if credentials_exist(home):
        log.info("Credentials already present at %s — skipping bootstrap.",
                 credentials_path(home))
        return True

    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set; it overrides subscription auth. "
            "Unset it before subscription login."
        )

    cfg = Config.load(project_path)
    token = cfg.credential("slack", "bot_token")
    if not token:
        raise RuntimeError(
            "No Slack bot_token configured — cannot post the login URL."
        )
    channel = channel or os.environ.get(LOGIN_CHANNEL_ENV, "")
    if not channel:
        raise RuntimeError(
            f"{LOGIN_CHANNEL_ENV} is unset — need a private channel to post the "
            "login URL into."
        )

    proc, master = spawn_login(home)
    try:
        url = _read_until_url(master, url_timeout)
        log.info("Captured login URL; posting to Slack channel %s.", channel)
        post_message(
            token, channel,
            "🔐 *modastack subscription login*\n"
            "Open this URL, authorize, then paste the code back "
            "*in this channel*:\n" + url,
        )
        code = wait_for_code(project_path, channel, timeout)
        _write_line(master, code)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            log.warning("claude login did not exit within 60s after the code.")
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
        "`fly ssh console` then `claude auth login --claudeai`."
    )
    try:
        post_message(token, channel, result_msg)
    except Exception as exc:  # noqa: BLE001 — best-effort status post
        log.warning("Could not post bootstrap result to Slack: %s", exc)
    return ok
