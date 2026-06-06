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
    pkg_dir = Path(__file__).resolve().parent.parent.parent
    es_dir = pkg_dir / "event-server"
    if es_dir.exists() and (es_dir / "package.json").exists():
        return es_dir
    raise FileNotFoundError(
        f"event-server directory not found at {es_dir}. "
        "Run from the modastack repo checkout for local event server."
    )


def _needs_build(es_dir: Path) -> bool:
    dist = es_dir / "dist" / "local.js"
    if not dist.exists():
        return True
    src_mtime = max(f.stat().st_mtime for f in (es_dir / "src").rglob("*.ts"))
    return dist.stat().st_mtime < src_mtime


def ensure_running(port: int, webhook_secret: str = "",
                   slack_signing_secret: str = "",
                   project_path: Path | None = None) -> None:
    """Start the local event server if not already running."""
    import urllib.request

    try:
        req = urllib.request.Request(f"http://localhost:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                log.info(f"Event server already running on port {port}")
                return
    except Exception:
        pass

    es_dir = _find_event_server_dir()

    if _needs_build(es_dir):
        log.info("Building local event server...")
        subprocess.run(
            ["npm", "run", "build:local"],
            cwd=str(es_dir),
            check=True,
            capture_output=True,
        )

    from modastack.sdk import get_project_root
    rp = project_path or get_project_root()
    if rp is None:
        raise RuntimeError("project_path required for event server")
    state = rp / ".modastack" / "state"
    state.mkdir(parents=True, exist_ok=True)
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
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=2):
                log.info(f"Event server started on port {port} (pid {proc.pid})")
                return
        except Exception:
            continue
    log.error("Event server failed to start within 15 seconds")


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
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    return result["deployment_id"], result["api_key"]
