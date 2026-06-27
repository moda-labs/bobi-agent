"""`bobi agent <name> ui <deployment>` — tunnel to a deployed team's in-container UI.

A Fly instance is dark (no public ingress); its agent UI binds the private 6PN
address inside the container. This helper does, in one command, what an operator
would otherwise do by hand — resolve the Fly app (same precedence as
`bobi deploy`), read the UI's port + token off the machine over `fly ssh`,
start `fly proxy`, and open the browser. Reading the token live also means a
token that rotated on the last restart just works.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_REMOTE_PORT = 8080


def _fly() -> str:
    from bobi.deploy import _fly_bin
    return _fly_bin()


def resolve_app(name: str | None, app: str | None,
                project_path: Path | None = None) -> str:
    """The Fly app to target. An explicit `--app` wins; otherwise resolve the
    deployment name through the same chain as `bobi deploy`
    (deployments/<name>.yaml › defaults.yaml › built-ins → `<fleet>-<name>`)."""
    if app:
        return app
    if not name:
        raise ValueError("a deployment name or --app is required")
    from bobi.deploy import load_deploy_config
    return load_deploy_config(project_path or Path.cwd(), name).app_name


def _ssh_bash(app: str, script: str) -> str:
    """Run a small read-only shell script via `fly ssh console`. '' on failure."""
    proc = subprocess.run(
        [_fly(), "ssh", "console", "-a", app, "-C",
         f"bash -lc {shlex.quote(script)}"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _remote_state_file(app: str, filename: str, *,
                       agent: str | None = None) -> str:
    agent_part = (
        f"agent={shlex.quote(agent)}; "
        if agent
        else ': "${BOBI_INSTANCE:?BOBI_INSTANCE is required}"; agent="${BOBI_INSTANCE}"; '
    )
    script = (
        agent_part
        + f'home="${{BOBI_HOME:?BOBI_HOME is required}}"; '
        + f'cat "$home/agents/$agent/run/state/{filename}"'
    )
    return _ssh_bash(app, script)


def fetch_token(app: str, *, agent: str | None = None) -> str:
    return _remote_state_file(app, "ui.token", agent=agent)


def fetch_remote_port(app: str, *, agent: str | None = None,
                      default: int = DEFAULT_REMOTE_PORT) -> int:
    raw = _remote_state_file(app, "ui.port", agent=agent)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def proxy_command(app: str, local_port: int, remote_port: int) -> list[str]:
    return [_fly(), "proxy", f"{local_port}:{remote_port}", "-a", app]


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.3)
    return False


def _get_agents(local_port: int, token: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{local_port}/api/agents",
        headers={"x-bobi-ui-token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _check(local_port: int, token: str) -> int:
    """One-shot reachability probe (for a non-gating canary smoke): GET
    /api/agents through the tunnel and report. 0 = reachable, 1 = not."""
    try:
        data = _get_agents(local_port, token)
    except Exception as e:                          # noqa: BLE001 — report any failure
        print(f"  ✗ agent UI check failed: {e}", file=sys.stderr)
        return 1
    names = [a.get("name", "?") for a in data.get("agents", [])]
    print(f"  ✓ agent UI reachable — {len(names)} agent(s): "
          f"{', '.join(names) or '(none)'}")
    return 0


def run(name: str | None = None, *, app: str | None = None,
        local_port: int | None = None, remote_port: int | None = None,
        open_browser: bool = True, check: bool = False) -> int:
    """Resolve → fetch token/port → `fly proxy` → open browser (or, with
    ``check``, probe /api/agents once and exit)."""
    from bobi import deploy
    deploy.preflight_fly_or_exit()                  # flyctl installed + logged in
    target = resolve_app(name, app)
    if not deploy.fly_app_exists(target):
        print(f"No Fly app '{target}'. Pass --app <app>, or check `fly apps list`.",
              file=sys.stderr)
        return 1

    rport = remote_port or fetch_remote_port(target, agent=name)
    token = fetch_token(target, agent=name)
    if not token:
        print(f"Couldn't read the UI token from '{target}'. The agent UI may not "
              "be enabled there — redeploy with BOBI_UI=1 (provisioned "
              "instances set it automatically).", file=sys.stderr)
        return 1
    lport = local_port or rport

    print(f"  Tunneling to {target} (remote :{rport}) …")
    proc = subprocess.Popen(proxy_command(target, lport, rport))
    try:
        if not _wait_for_port(lport):
            print(f"`fly proxy` didn't open localhost:{lport} in time.",
                  file=sys.stderr)
            return 1
        if check:
            return _check(lport, token)
        url = f"http://localhost:{lport}/?n={token}"
        print(f"\n  {target} agent UI → {url}\n  (Ctrl-C to close the tunnel)\n")
        if open_browser:
            webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0
