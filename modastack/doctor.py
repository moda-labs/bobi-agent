"""System health checks — manager, event server, repos, workflows."""

from __future__ import annotations

import shutil

from modastack.browser import CheckResult
from modastack.config import GlobalConfig


def run_doctor() -> list[CheckResult]:
    results = []

    results.append(_check_claude_cli())
    results.append(_check_global_config())
    results.append(_check_repos())
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


def _check_global_config() -> CheckResult:
    from modastack.config import GLOBAL_CONFIG_PATH
    if GLOBAL_CONFIG_PATH.exists():
        return CheckResult("Global config", ok=True,
                           detail=str(GLOBAL_CONFIG_PATH))
    return CheckResult("Global config", ok=False,
                       detail="missing",
                       hint="Run `modastack start` to create it")


def _check_repos() -> CheckResult:
    try:
        config = GlobalConfig.load()
        if not config.repos:
            return CheckResult("Registered repos", ok=False,
                               detail="none registered",
                               hint="Run `modastack setup <repo-path>`")
        missing = [p for p in config.repos if not p.exists()]
        if missing:
            return CheckResult("Registered repos", ok=False,
                               detail=f"{len(missing)} missing: {', '.join(str(p) for p in missing)}")
        return CheckResult("Registered repos", ok=True,
                           detail=f"{len(config.repos)} registered")
    except Exception as e:
        return CheckResult("Registered repos", ok=False, detail=str(e))


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

    config = GlobalConfig.load()
    port = config.webhook_port

    # If cloud config is present, report that instead
    if config.event_server_url and config.event_server_api_key:
        return CheckResult("Event server", ok=True,
                           detail=f"cloud mode ({config.event_server_url})")

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
    """Check if events have been received recently."""
    import datetime
    import json
    import time

    from modastack.manager.events.event_client import _state_path

    events_file = _state_path("events.jsonl")
    if not events_file.exists():
        return CheckResult("Recent events", ok=True,
                           detail="no events yet (normal for new installs)")

    one_hour_ago = time.time() - 3600
    recent_count = 0
    try:
        for line in events_file.read_text().splitlines()[-100:]:
            entry = json.loads(line)
            ts = entry.get("timestamp", "")
            if ts:
                dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.timestamp() > one_hour_ago:
                    recent_count += 1
    except Exception:
        pass

    if recent_count > 0:
        return CheckResult("Recent events", ok=True,
                           detail=f"{recent_count} events in the last hour")
    return CheckResult("Recent events", ok=True,
                       detail="no events in the last hour")
