"""Subscription-login bootstrap (containerized-23 / #343; brain-aware in #485).

At container first boot in subscription auth mode with no credentials on the
volume, drive the brain's login CLI under a pty, scrape the sign-in URL, post it
to a private Slack channel via the bot token already in env, and land the OAuth
credentials on the volume. Two flow shapes, picked per brain:

- **Claude** (``claude auth login --claudeai``, *paste-back*): scrape the URL,
  post it, wait for the human to paste the auth code back — which arrives as a
  Slack message event over the event bus — and write it into the pty.
- **Codex** (``codex login --device-auth``, *device-poll*): scrape the sign-in
  URL **and** the one-time code, post both, then just wait — the CLI polls the
  token endpoint until the human authorizes; nothing is pasted back.

Refresh-token rotation makes this a once-per-machine ceremony
(CONTAINERIZED_INSTANCES.md §6.1); the manual fallback is ``fly ssh console`` +
the brain's login command. The live round-trip needs a real Worker + Slack and
is exercised in the deployed environment; the mechanism here is unit-tested with
the pty, the Slack post, and the event source faked — see
tests/test_auth_bootstrap.py.
"""
from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from modastack.brain import BRAIN_ENV, set_process_brain
from modastack.config import Config
from modastack.slack import post_slack_message

log = logging.getLogger(__name__)

# Env var naming the private Slack channel to post the login URL into. A
# private channel is a hard requirement (§6.1, C23): the code is single-use but
# grants the login to whoever pastes it first.
LOGIN_CHANNEL_ENV = "MODASTACK_LOGIN_CHANNEL"

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
    code_re: "re.Pattern | None" = None  # device_poll: also scrape the one-time code


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
    """The login spec for this process's brain (``MODASTACK_BRAIN``; default claude)."""
    kind = os.environ.get(BRAIN_ENV) or "claude"
    return _SPECS.get(kind, _SPECS["claude"])


def credentials_path(home: Path | None = None) -> Path:
    """Path to the active brain's subscription OAuth credentials on the volume."""
    base = home or Path(os.environ.get("HOME", str(Path.home())))
    return Path(base, *_active_spec().creds_relpath)


def credentials_exist(home: Path | None = None) -> bool:
    return credentials_path(home).is_file()


def needs_bootstrap(home: Path | None = None) -> bool:
    """True iff we're in subscription mode with no credentials yet."""
    if os.environ.get("MODASTACK_AUTH", "api_key") != "subscription":
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

    # Ensure the Worker holds the bot token so it ingests this channel's messages.
    register_slack_workspaces(es_url, cfg)
    team_id, _ = _slack_auth_info(token)
    if not team_id:
        raise RuntimeError("could not resolve Slack team_id from bot_token.")
    topic = f"slack:{team_id}"

    bubble = ensure_bubble(es_url, project_path)
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
    # all the right ones. Loading cfg here also seeds MODASTACK_BRAIN for the
    # spec lookups below (and the spawned login subprocess).
    cfg = Config.load(project_path)
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
    # Accept a readable '#name' in the config; resolve to the ID the post + the
    # event-bus filter both need.
    from modastack.slack import resolve_channel_id
    channel = resolve_channel_id(token, channel)

    login_cmd_str = " ".join(spec.login_cmd)
    proc, master = spawn_login(home)
    try:
        if spec.flow == "paste_back":
            # Claude: scrape the URL, post it, wait for the human to paste the
            # code back over Slack, write it into the pty.
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
                log.warning("login did not exit within 60s after the code.")
        else:
            # Codex device-poll: scrape the URL **and** the one-time code, post
            # both, then just wait — the CLI polls until the human authorizes;
            # nothing is pasted back.
            url, code = scrape_login(master, url_timeout, spec)
            log.info("Captured device URL + code; posting to Slack channel %s.",
                     channel)
            post_message(
                token, channel,
                "🔐 *modastack subscription login*\n"
                "Open this link, sign in, then enter the one-time code:\n"
                f"{url}\n"
                f"Code: `{code}`\n"
                "_Waiting for you to authorize…_",
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
        post_message(token, channel, result_msg)
    except Exception as exc:  # noqa: BLE001 — best-effort status post
        log.warning("Could not post bootstrap result to Slack: %s", exc)
    return ok
