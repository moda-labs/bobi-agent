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


def register(base_url: str, name: str,
             subscriptions: list[str]) -> tuple[str, str]:
    """Register a deployment. Returns (deployment_id, api_key)."""
    from modastack import http as pooled

    resp = pooled.post(
        f"{base_url}/deployments",
        json={"name": name, "subscriptions": subscriptions},
        timeout=15.0,
    )
    result = resp.json()
    return result["deployment_id"], result["api_key"]


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
