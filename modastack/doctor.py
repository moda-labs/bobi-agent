"""System health checks — manager, event server, projects, workflows."""

from __future__ import annotations

import shutil

from modastack.browser import CheckResult


def run_doctor() -> list[CheckResult]:
    results = []

    results.append(_check_claude_cli())
    results.append(_check_project_config())
    results.append(_check_local_config())
    results.append(_check_workflows())
    results.append(_check_event_server())
    results.append(_check_recent_events())

    return results


def _check_claude_cli() -> CheckResult:
    if shutil.which("claude"):
        return CheckResult("Claude CLI", ok=True, detail="found")
    return CheckResult("Claude CLI", ok=False,
                       detail="not found in PATH",
                       hint="Install Claude Code: https://docs.anthropic.com/en/docs/claude-code")


def _check_project_config() -> CheckResult:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Project config", ok=False,
                           detail="not inside a modastack project",
                           hint="Run from a directory with .modastack/config.yaml")
    config_path = root / ".modastack" / "config.yaml"
    if config_path.exists():
        return CheckResult("Project config", ok=True, detail=str(config_path))
    return CheckResult("Project config", ok=False,
                       detail="missing .modastack/config.yaml",
                       hint="Run `modastack init` to create it")


def _check_local_config() -> CheckResult:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Local config", ok=False, detail="no project detected")
    local_path = root / ".modastack" / "local.yaml"
    if local_path.exists():
        return CheckResult("Local config", ok=True, detail=str(local_path))
    return CheckResult("Local config", ok=False,
                       detail="missing .modastack/local.yaml",
                       hint="Run `modastack init` to create operator config")


def _check_workflows() -> CheckResult:
    try:
        from modastack.workflow.triggers import WorkflowDispatcher
        d = WorkflowDispatcher()
        d.load_all_workflows()
        names = [wf.name for wf, _ in d.workflows]
        if not names:
            return CheckResult("Workflows", ok=False,
                               detail="none found",
                               hint="Add workflows to .modastack/workflows/")
        return CheckResult("Workflows", ok=True,
                           detail=f"{len(names)} loaded: {', '.join(names)}")
    except Exception as e:
        return CheckResult("Workflows", ok=False, detail=str(e))


def _check_event_server() -> CheckResult:
    """Probe the event server /health endpoint."""
    import json
    import urllib.request

    from modastack.config import LocalConfig, ProjectConfig
    from modastack.sdk import get_project_root

    root = get_project_root()
    if root:
        try:
            rc = ProjectConfig.from_file(root)
            local = LocalConfig.load(root)
            if rc.event_server_url and local.event_server_api_key:
                return CheckResult("Event server", ok=True,
                                   detail=f"remote ({rc.event_server_url})")
        except FileNotFoundError:
            pass

    port = 8080
    try:
        req = urllib.request.Request(f"http://localhost:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            mode = data.get("mode", "unknown")
            deployments = data.get("deployments", 0)
            return CheckResult("Event server", ok=True,
                               detail=f"running on port {port} (mode={mode}, deployments={deployments})")
    except Exception:
        return CheckResult("Event server", ok=False,
                           detail=f"not running on port {port}",
                           hint="`modastack event-server start` or `modastack start` will auto-launch")


def _check_recent_events() -> CheckResult:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Recent events", ok=False, detail="no project detected")
    events_file = root / ".modastack" / "state" / "events.jsonl"
    if not events_file.exists():
        return CheckResult("Recent events", ok=True, detail="no events yet")
    lines = events_file.read_text().strip().splitlines()
    return CheckResult("Recent events", ok=True, detail=f"{len(lines)} events logged")
