"""System health checks for `modastack doctor`.

Each check returns a CheckResult (reused from browser module).
run_doctor() returns the full checklist; the CLI renders it.
"""

from __future__ import annotations

import socket

from .browser import CheckResult
from .config import GlobalConfig


def check_manager_running() -> CheckResult:
    """Check whether the manager session is alive."""
    try:
        from .manager.session import is_alive
        alive = is_alive()
    except Exception as e:
        return CheckResult(
            name="Manager session",
            ok=False,
            detail=f"Could not check manager state: {e}",
            hint="Run `modastack start` to launch the manager.",
        )

    if alive:
        return CheckResult(name="Manager session", ok=True, detail="Running")
    return CheckResult(
        name="Manager session",
        ok=False,
        detail="Not running",
        hint="Run `modastack start` to launch the manager.",
    )


def check_event_server() -> CheckResult:
    """Check that the event server WebSocket endpoint is reachable."""
    config = GlobalConfig.load()

    if not config.event_server_url:
        return CheckResult(
            name="Event server",
            ok=False,
            detail="No event_server URL configured",
            hint="Set event_server.url in ~/.modastack/config.yaml",
        )

    try:
        from urllib.parse import urlparse
        parsed = urlparse(config.event_server_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        return CheckResult(
            name="Event server",
            ok=True,
            detail=f"Reachable at {config.event_server_url}",
        )
    except Exception as e:
        return CheckResult(
            name="Event server",
            ok=False,
            detail=f"Cannot reach {config.event_server_url}: {e}",
            hint="Check your network or event_server config.",
        )


def check_dashboard() -> CheckResult:
    """Check that the dashboard HTTP server is responding on port 8095."""
    try:
        from urllib.request import urlopen
        resp = urlopen("http://localhost:8095/api/status", timeout=3)
        resp.read()
        return CheckResult(
            name="Dashboard",
            ok=True,
            detail="Accessible on http://localhost:8095",
        )
    except Exception:
        return CheckResult(
            name="Dashboard",
            ok=False,
            detail="Not responding on http://localhost:8095",
            hint="The dashboard starts automatically with `modastack start`.",
        )


def check_repos() -> CheckResult:
    """Check that all registered repos exist on disk."""
    config = GlobalConfig.load()

    if not config.repos:
        return CheckResult(
            name="Registered repos",
            ok=True,
            detail="No repos registered",
        )

    missing = [str(p) for p in config.repos if not p.exists()]

    if not missing:
        return CheckResult(
            name="Registered repos",
            ok=True,
            detail=f"All {len(config.repos)} repos exist on disk",
        )
    return CheckResult(
        name="Registered repos",
        ok=False,
        detail=f"Missing: {', '.join(missing)}",
        hint="Run `modastack register <path>` to fix or remove stale entries.",
    )


def check_workflows() -> CheckResult:
    """Check that all workflow YAML files parse without errors."""
    from .workflow.triggers import WORKFLOWS_DIR, USER_WORKFLOWS_DIR
    from .workflow.schema import load_workflow

    errors: list[str] = []
    count = 0

    for d in (WORKFLOWS_DIR, USER_WORKFLOWS_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            count += 1
            try:
                load_workflow(f)
            except Exception as e:
                errors.append(f"{f.name}: {e}")

    if errors:
        return CheckResult(
            name="Workflow YAML",
            ok=False,
            detail=f"{len(errors)} error(s): {'; '.join(errors)}",
            hint="Fix the YAML syntax in the listed workflow files.",
        )
    return CheckResult(
        name="Workflow YAML",
        ok=True,
        detail=f"{count} workflow(s) parsed OK" if count else "No workflow files found",
    )


def run_doctor() -> list[CheckResult]:
    """Run all system health checks and return the results."""
    return [
        check_manager_running(),
        check_event_server(),
        check_dashboard(),
        check_repos(),
        check_workflows(),
    ]
