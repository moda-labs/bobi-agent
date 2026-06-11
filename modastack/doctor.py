"""System health checks — manager, event server, projects, workflows."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass


@dataclass
class CheckResult:
    """Outcome of a single health check."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # Set when the failure is specifically the AppArmor userns sandbox block,
    # so callers can offer the targeted fix.
    sandbox_error: bool = False


def run_doctor() -> list[CheckResult]:
    logging.getLogger("modastack").setLevel(logging.WARNING)

    results = []

    results.append(_check_claude_cli())
    results.append(_check_claude_auth())
    results.append(_check_local_config())
    results.append(_check_install_integrity())
    results.extend(_check_services())
    results.append(_check_workflows())
    results.append(_check_event_server())
    results.append(_check_recent_events())
    results.append(_check_memory())

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


def _check_install_integrity() -> CheckResult:
    """Flag edits to the installed .modastack/ image.

    The installed copy is frozen — regenerated verbatim by `modastack
    install` — so hand-edits are silently lost on the next install.
    Compare on-disk files against the hashes recorded at install time.
    """
    import hashlib
    import json

    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Installed team", ok=True, detail="no project")
    dest = root / ".modastack"
    manifest_path = dest / "install-manifest.json"
    if not manifest_path.exists():
        return CheckResult("Installed team", ok=True,
                           detail="no install manifest (pre-0.12 install)")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckResult("Installed team", ok=False,
                           detail="unreadable install manifest",
                           hint="Re-run `modastack install`")
    if not manifest.get("frozen", True):
        return CheckResult("Installed team", ok=True,
                           detail=f"{manifest.get('agent', '?')} (downloaded — editable)")
    drifted = []
    for rel, digest in manifest.get("files", {}).items():
        f = dest / rel
        if not f.is_file():
            drifted.append(f"{rel} (missing)")
        elif hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            drifted.append(rel)
    if drifted:
        shown = ", ".join(drifted[:3]) + ("…" if len(drifted) > 3 else "")
        return CheckResult(
            "Installed team", ok=False,
            detail=f"{len(drifted)} file(s) differ from installed pack: {shown}",
            hint="Edits to .modastack/ are lost on reinstall — edit the "
                 "pack source and re-run `modastack install`")
    return CheckResult("Installed team", ok=True,
                       detail=f"{manifest.get('agent', '?')} (frozen, clean)")


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
    from modastack.config import Config, load_deployment_state
    from modastack.events.server import health
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

    url = "http://localhost:8080"
    if health(url):
        return CheckResult("Event server", ok=True, detail=url)
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


def _check_memory() -> CheckResult:
    """Check agent decision logs — flag agents with empty current-state blocks."""
    from modastack.sdk import get_project_root
    root = get_project_root()
    if not root:
        return CheckResult("Decision log", ok=True, detail="no project detected")
    memory_root = root / ".modastack" / "state" / "memory"
    if not memory_root.is_dir():
        return CheckResult("Decision log", ok=True, detail="no decision logs yet")

    agents = []
    empty = []
    for agent_dir in sorted(memory_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        agents.append(agent_dir.name)
        index = agent_dir / "INDEX.md"
        if not index.is_file():
            empty.append(agent_dir.name)
            continue
        content = index.read_text().strip()
        # Check if the YAML frontmatter block has any content
        if content in ("", "---\n---", "---\n---\n"):
            empty.append(agent_dir.name)

    if not agents:
        return CheckResult("Decision log", ok=True, detail="no decision logs yet")

    if empty:
        shown = ", ".join(empty[:3]) + ("..." if len(empty) > 3 else "")
        has_populated = len(agents) > len(empty)
        return CheckResult(
            "Decision log",
            ok=has_populated,  # only fail if ALL logs are empty (likely drift)
            detail=f"{len(empty)} agent(s) with empty decision logs: {shown}",
            hint="Agents should record decisions in .modastack/state/memory/<session>/INDEX.md")

    return CheckResult(
        "Decision log", ok=True,
        detail=f"{len(agents)} agent(s) with decision logs")
