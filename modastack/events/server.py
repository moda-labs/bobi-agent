"""Local event server launcher.

The event server codebase is TypeScript in event-server/. This module
provides Python helpers to start it locally and register deployments.
The same TypeScript core runs on Cloudflare Workers (production) or
Node.js (local development).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _find_event_server_dir() -> Path:
    pkg_dir = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_dir / "event-server",         # bundled in the installed package
        pkg_dir.parent / "event-server",  # repo checkout
    ]
    for es_dir in candidates:
        if (es_dir / "package.json").exists():
            return es_dir
    raise FileNotFoundError(
        "event-server directory not found (checked "
        + ", ".join(str(c) for c in candidates) + ")."
    )


def _needs_build(es_dir: Path) -> bool:
    dist = es_dir / "dist" / "local.js"
    if not dist.exists():
        return True
    src_mtime = max(f.stat().st_mtime for f in (es_dir / "src").rglob("*.ts"))
    return dist.stat().st_mtime < src_mtime


def health(base_url: str, timeout: float = 2) -> dict | None:
    """Probe an event server's /health endpoint.

    Returns the parsed health payload when the server reports ok, else None.
    The single definition of "what counts as healthy" — used by ensure_running,
    `modastack stop`, `modastack event-server status`, and doctor.
    """
    from modastack import http as pooled

    try:
        resp = pooled.get(f"{base_url}/health", timeout=timeout)
        data = resp.json()
        return data if data.get("status") == "ok" else None
    except Exception:
        return None


def _run_npm(args: list[str], es_dir: Path) -> None:
    """Run an npm command, surfacing its output on failure.

    npm failures here used to raise a bare CalledProcessError with the
    output captured but never shown — the real cause (e.g. ENOSPC)
    was invisible in manager.log.
    """
    result = subprocess.run(
        args, cwd=str(es_dir), capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        log.error(f"{' '.join(args)} failed (exit {result.returncode}):\n{detail}")
        raise RuntimeError(
            f"{' '.join(args)} failed (exit {result.returncode}): "
            f"{detail or 'no output'}"
        )


def ensure_running(port: int, webhook_secret: str = "",
                   slack_signing_secret: str = "",
                   project_path: Path | None = None) -> str:
    """Start the local event server if not already running.

    Returns "connected" if an existing server was found, "started" if
    a new one was launched.
    """
    if health(f"http://localhost:{port}"):
        log.info(f"Event server already running on port {port}")
        return "connected"

    es_dir = _find_event_server_dir()

    if not (es_dir / "node_modules").exists():
        log.info("Installing event server dependencies...")
        _run_npm(["npm", "install", "--no-audit", "--no-fund"], es_dir)

    if _needs_build(es_dir):
        log.info("Building local event server...")
        _run_npm(["npm", "run", "build:local"], es_dir)

    from modastack import paths
    state = paths.state_dir(project_path)
    log_file = state / "event-server.log"
    pid_file = state / "event-server.pid"

    env = dict(os.environ)
    env["MODASTACK_ES_PORT"] = str(port)
    if webhook_secret:
        env["MODASTACK_ES_WEBHOOK_SECRET"] = webhook_secret
    if slack_signing_secret:
        env["MODASTACK_ES_SLACK_SIGNING_SECRET"] = slack_signing_secret

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            ["node", str(es_dir / "dist" / "local.js")],
            stdout=lf, stderr=lf,
            env=env, start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))

    for _ in range(30):
        time.sleep(0.5)
        if health(f"http://localhost:{port}"):
            log.info(f"Event server started on port {port} (pid {proc.pid})")
            return "started"
    log.error("Event server failed to start within 15 seconds")
    return "failed"


class BubbleRejected(Exception):
    """A signed JOIN was rejected (403) — the server does not recognize the
    bubble (e.g. it restarted and lost its in-memory bubbles). The caller
    should re-mint and re-join."""


def _post_register(base_url: str, name: str, subscriptions: list[str],
                   bubble_id: str = "", bubble_key: str = "") -> dict:
    """POST /deployments. MINT when no bubble_key (server generates a bubble +
    returns its key once); JOIN when signed with an existing bubble's key.

    Signs over the exact transmitted bytes (content=, not json=) so the
    server's HMAC verification reproduces the signature. Raises BubbleRejected
    on a 403 join so callers can re-mint.
    """
    from modastack import http as pooled
    from modastack.events.signing import serialize_body, sign_headers

    body = serialize_body({"name": name, "subscriptions": subscriptions})
    headers = {"Content-Type": "application/json"}
    if bubble_key:
        headers.update(sign_headers(bubble_id, bubble_key, "POST", "/deployments", body))

    resp = pooled.post(
        f"{base_url}/deployments",
        content=body,
        headers=headers,
        timeout=15.0,
    )
    if resp.status_code == 403:
        raise BubbleRejected(f"join rejected for bubble {bubble_id}")
    return resp.json()


def register(base_url: str, name: str, subscriptions: list[str],
             bubble_id: str = "", bubble_key: str = "") -> tuple[str, str]:
    """JOIN a deployment into the instance's bubble. Returns (deployment_id,
    api_key). Callers pass the bubble credential from :func:`ensure_bubble`;
    the bubble must already exist (mint happens only in ensure_bubble)."""
    result = _post_register(base_url, name, subscriptions, bubble_id, bubble_key)
    return result["deployment_id"], result["api_key"]


def _is_loopback_or_tls(base_url: str) -> bool:
    """Whether the bubble key may safely transit to this URL at mint time."""
    from urllib.parse import urlsplit

    if base_url.startswith("https://"):
        return True
    host = urlsplit(base_url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def ensure_bubble(base_url: str, project_path: Path,
                  force_remint_of: str = "") -> dict:
    """Return the instance's bubble credential, minting it once if absent.

    The SINGLE seam all deployments go through: every session/agent/reply
    channel JOINs the bubble this returns. Minting is lock-protected
    (O_CREAT|O_EXCL) so two concurrent first-registrations converge on one
    bubble instead of splitting the instance. Minting transmits the key once,
    so it is refused over a non-loopback cleartext URL.

    ``force_remint_of`` is a compare-and-swap: when a JOIN was rejected because
    the server forgot the bubble (restart), the caller passes the stale
    bubble_id. We re-mint ONLY if the on-disk bubble still matches it — if
    another session already re-minted, we return the new one instead of
    splitting the instance into a third bubble.
    """
    import os

    from modastack.config import load_bubble_state, save_bubble_state, bubble_state_path

    existing = load_bubble_state(project_path)
    if existing.get("bubble_id") and existing.get("bubble_key"):
        if not force_remint_of or existing["bubble_id"] != force_remint_of:
            return existing
        # else: caller flagged this bubble stale — fall through to re-mint.

    lock_path = bubble_state_path(project_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        # Another process is minting — wait for it to publish bubble.json.
        # The budget MUST exceed the mint's HTTP timeout (_post_register, 15s)
        # plus margin — otherwise a slow-but-alive first minter outlasts the
        # wait and the waiter forks its own bubble. 30s covers it; only a
        # crashed minter holding the lock falls through to mint ourselves.
        for _ in range(300):
            time.sleep(0.1)
            existing = load_bubble_state(project_path)
            if existing.get("bubble_id") and existing["bubble_id"] != force_remint_of:
                return existing
        # Stale lock (minter died) — fall through and mint ourselves.

    try:
        existing = load_bubble_state(project_path)
        if existing.get("bubble_id") and existing["bubble_id"] != force_remint_of:
            return existing  # someone already (re)minted under the lock

        if not _is_loopback_or_tls(base_url):
            raise RuntimeError(
                f"Refusing to mint a bubble over cleartext remote URL {base_url} "
                "— the bubble key would transit in the clear. Use https:// or a "
                "loopback event server."
            )

        # MINT via a throwaway bootstrap deployment (the server mints a bubble
        # as part of registration). One idle deployment per bubble — negligible.
        result = _post_register(base_url, "bubble-bootstrap", ["_bootstrap"])
        save_bubble_state(project_path, result["bubble_id"], result["bubble_key"])
        return load_bubble_state(project_path)
    finally:
        lock_path.unlink(missing_ok=True)


def _slack_auth_info(token: str) -> tuple[str, str]:
    """Resolve (team_id, bot_id) from a bot token via auth.test."""
    from modastack import http as pooled

    try:
        resp = pooled.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("team_id", "") or "", data.get("bot_id", "") or ""
    except Exception as e:  # best-effort — never block startup
        log.debug("Slack auth.test failed during workspace registration: %s", e)
    return "", ""


def register_slack_workspaces(base_url: str, cfg) -> list[str]:
    """Register the agent's Slack workspace(s) with the event server.

    The event server uses the registered ``bot_id`` to skip the bot's OWN
    messages (``event.bot_id == selfBotId``). Without this, an agent's own
    Slack reply is re-ingested as a fresh inbound event and it loops on
    itself. This wires the missing registration so that loop prevention
    actually engages. Best-effort: logs and continues on any failure so a
    registration hiccup never blocks startup. Returns the workspace ids
    successfully registered.
    """
    from modastack import http as pooled

    try:
        token = cfg.credential("slack", "bot_token")
    except Exception:
        token = ""
    if not token:
        return []
    team_id, bot_id = _slack_auth_info(token)
    if not team_id:
        return []
    try:
        # Send bot_id explicitly when known: the server's own auth.test
        # fallback is best-effort, and a registration without bot_id
        # silently disables self-reply filtering for the whole workspace.
        record: dict = {"workspace_id": team_id, "bot_token": token}
        if bot_id:
            record["bot_id"] = bot_id
        pooled.post(
            f"{base_url}/slack/workspaces",
            json=record,
            timeout=10.0,
        )
        log.info(
            "Registered Slack workspace %s with event server "
            "(self-reply loop prevention)", team_id,
        )
        return [team_id]
    except Exception as e:
        log.warning("Slack workspace registration failed for %s: %s", team_id, e)
        return []
