"""System health checks — manager, event server, projects, workflows."""

from __future__ import annotations

import logging
import shutil

from modastack.browser import CheckResult


def run_doctor() -> list[CheckResult]:
    logging.getLogger("modastack").setLevel(logging.WARNING)

    results = []

    results.append(_check_claude_cli())
    results.append(_check_claude_auth())
    results.append(_check_local_config())
    results.extend(_check_services())
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


def _check_claude_auth() -> CheckResult:
    """Verify Claude can authenticate by running a minimal query."""
    import subprocess
    try:
        result = subprocess.run(
            ["claude", "--print", "hi"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return CheckResult("Claude auth", ok=True, detail="authenticated")
        stderr = result.stderr.strip()
        if "401" in stderr or "auth" in stderr.lower():
            return CheckResult("Claude auth", ok=False,
                               detail="authentication failed (401)",
                               hint="Run `claude auth login` to re-authenticate")
        return CheckResult("Claude auth", ok=False,
                           detail=f"failed: {stderr[:100]}",
                           hint="Run `claude auth login`")
    except FileNotFoundError:
        return CheckResult("Claude auth", ok=False, detail="claude not installed")
    except subprocess.TimeoutExpired:
        return CheckResult("Claude auth", ok=False,
                           detail="timed out",
                           hint="Check network connectivity")


def _check_project_config() -> CheckResult:
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Project", ok=False,
                           detail="project root not set",
                           hint="Run `modastack start <agent>` from a project directory")
    modastack_dir = root / ".modastack"
    if modastack_dir.is_dir():
        return CheckResult("Project", ok=True, detail=str(root))
    return CheckResult("Project", ok=True,
                       detail=f"{root} (no .modastack/ yet — created on first start)")


def _check_local_config() -> CheckResult:
    from modastack.config import _project_config_path
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Project config", ok=False,
                           detail="no project detected",
                           hint="Run from a project directory with .modastack/")
    config_path = _project_config_path(root)
    if config_path.exists():
        return CheckResult("Project config", ok=True, detail=str(config_path))
    return CheckResult("Project config", ok=False,
                       detail=f"missing {config_path}",
                       hint="Create .modastack/agent.yaml with entry_point, services, and credentials")


def _check_services() -> list[CheckResult]:
    """Run service validation — native credentials, Venn, MCP servers."""
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return []
    try:
        from modastack.config import load_dotenv
        load_dotenv(root)
        from modastack.validate import validate_config
        result = validate_config(root)
        return [
            CheckResult(c.name, ok=c.ok, detail=c.detail, hint=c.hint)
            for c in result.checks
        ]
    except Exception as e:
        return [CheckResult("Services", ok=False, detail=f"validation error: {e}")]


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

    from modastack.config import Config, load_deployment_state
    from modastack.sdk import get_project_root

    root = get_project_root()
    if root:
        try:
            cfg = Config.load(root)
            state = load_deployment_state(root)
            if cfg.event_server_url and state.get("api_key"):
                return CheckResult("Event server", ok=True,
                                   detail=f"remote ({cfg.event_server_url})")
        except FileNotFoundError:
            pass

    port = 8080
    try:
        url = f"http://localhost:{port}"
        req = urllib.request.Request(f"{url}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return CheckResult("Event server", ok=True,
                               detail=f"{url}")
    except Exception:
        return CheckResult("Event server", ok=False,
                           detail="not running",
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
