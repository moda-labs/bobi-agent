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
    import urllib.request

    try:
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
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

    from modastack.sdk import get_project_root, state_dir
    rp = project_path or get_project_root()
    if rp is None:
        raise RuntimeError("project_path required for event server")
    state = state_dir(rp)
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
    import urllib.request

    data = json.dumps({"name": name, "subscriptions": subscriptions}).encode()
    req = urllib.request.Request(
        f"{base_url}/deployments",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "modastack"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    return result["deployment_id"], result["api_key"]
