"""System health checks — manager, event server, projects, workflows."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from modastack.paths import bound_root


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
    results.append(_check_codex_cli())
    results.append(_check_codex_auth())
    results.append(_check_local_config())
    results.append(_check_single_root())
    results.append(_check_install_integrity())
    results.extend(_check_package_requires())
    results.extend(_check_services())
    results.append(_check_workflows())
    results.append(_check_bubble_auth())
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


def _check_codex_cli() -> CheckResult:
    """Check that the Codex CLI is installed and on PATH."""
    if shutil.which("codex"):
        return CheckResult("Codex CLI", ok=True, detail="found")
    return CheckResult(
        "Codex CLI", ok=False,
        detail="not found in PATH",
        hint="Install Codex CLI: npm install -g @openai/codex")


def _check_codex_auth() -> CheckResult:
    """Verify Codex CLI can authenticate.

    Codex supports two auth modes: ChatGPT subscription login (primary)
    and OPENAI_API_KEY env var (fallback).  We run ``codex --version``
    first to confirm the binary works, then check for a usable API key
    or cached session.
    """
    import os
    import subprocess

    if not shutil.which("codex"):
        return CheckResult("Codex auth", ok=False, detail="codex not installed")

    # Check if OPENAI_API_KEY is set (API-key fallback auth)
    has_api_key = bool(os.environ.get("OPENAI_API_KEY"))

    # Verify the CLI is functional
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return CheckResult(
                "Codex auth", ok=False,
                detail=f"codex unhealthy: {stderr[:100]}",
                hint="Reinstall: npm install -g @openai/codex")
    except subprocess.TimeoutExpired:
        return CheckResult("Codex auth", ok=False,
                           detail="timed out",
                           hint="Check network connectivity")
    except FileNotFoundError:
        return CheckResult("Codex auth", ok=False, detail="codex not installed")

    # Try a lightweight exec to verify auth works
    try:
        result = subprocess.run(
            ["codex", "exec", "echo hello"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            auth_mode = "API key" if has_api_key else "subscription"
            return CheckResult("Codex auth", ok=True,
                               detail=f"authenticated ({auth_mode})")
        stderr = result.stderr.strip().lower()
        if "auth" in stderr or "api key" in stderr or "401" in stderr or "login" in stderr:
            hint = ("Set OPENAI_API_KEY in .modastack/.env or run "
                    "`codex auth login` for ChatGPT subscription auth")
            return CheckResult("Codex auth", ok=False,
                               detail="authentication failed",
                               hint=hint)
        return CheckResult("Codex auth", ok=False,
                           detail=f"exec failed: {result.stderr.strip()[:100]}",
                           hint="Set OPENAI_API_KEY or run `codex auth login`")
    except subprocess.TimeoutExpired:
        return CheckResult("Codex auth", ok=False,
                           detail="exec timed out (30s)",
                           hint="Check network connectivity and API key validity")


def _check_install_integrity() -> CheckResult:
    """Flag edits to the installed .modastack/ image.

    The installed copy is frozen — regenerated verbatim by `modastack
    install` — so hand-edits are silently lost on the next install.
    Compare on-disk files against the hashes recorded at install time.
    """
    import hashlib
    import json

    root = bound_root()
    if not root:
        return CheckResult("Installed team", ok=True, detail="no project")
    from modastack import paths
    dest = paths.modastack_dir(root)
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


def _check_package_requires() -> list[CheckResult]:
    """Check host-level dependencies declared in agent.yaml requires: block."""
    from modastack.config import Config, run_requires_checks

    root = bound_root()
    if not root:
        return []
    try:
        cfg = Config.load(root)
    except Exception:
        return []
    if not cfg.requires:
        return []

    results = []
    for entry, ok, detail in run_requires_checks(cfg.requires):
        if ok:
            results.append(CheckResult(
                f"Requires: {entry.name}", ok=True, detail="healthy"))
        else:
            hint = f"Fix: {entry.fix}" if entry.fix else ""
            results.append(CheckResult(
                f"Requires: {entry.name}", ok=False,
                detail=entry.why or detail,
                hint=hint))
    return results


def _check_local_config() -> CheckResult:
    from modastack.config import _project_config_path
    root = bound_root()
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


def _check_single_root() -> CheckResult:
    """Exactly one .modastack/ per installation — flag strays below it.

    Stray .modastack/ dirs in repo checkouts under the root are leftovers
    from the old cwd-as-root resolution. They hold orphaned state at best;
    at worst one contains an agent.yaml and captures project-root
    resolution for anything launched from that subtree.
    """
    root = bound_root()
    if not root:
        return CheckResult("Single .modastack root", ok=True,
                           detail="no project root bound")
    import os
    state_only, installs = [], []
    for dirpath, dirnames, _files in os.walk(root):
        cur = Path(dirpath)
        if cur == root:
            # The root's own .modastack (worktrees, sessions, ...) is the
            # installation, not a stray; heavy trees aren't worth walking.
            dirnames[:] = [d for d in dirnames
                           if d not in (".modastack", ".git", "node_modules")]
            continue
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        if ".modastack" in dirnames:
            rel = str(cur.relative_to(root))
            if (cur / ".modastack" / "agent.yaml").is_file():
                installs.append(rel)
            else:
                state_only.append(rel)
            dirnames.remove(".modastack")
    if not state_only and not installs:
        return CheckResult("Single .modastack root", ok=True,
                           detail="no stray .modastack dirs below root")
    parts, hints = [], []
    if installs:
        parts.append("nested installs (agent.yaml — these CAPTURE root "
                      "resolution for anything below them): "
                      + ", ".join(sorted(installs)))
        hints.append("Nested installs may be deliberate (`modastack install` "
                     "in a subdir) — if not, they are hijack vectors; remove "
                     "the marker or the dir.")
    if state_only:
        parts.append("state-only strays: " + ", ".join(sorted(state_only)))
        hints.append("State-only dirs are leftovers from cwd-bound agents — "
                     "they may hold live cursors/sessions from before the "
                     "upgrade; tar them up before removing.")
    return CheckResult(
        "Single .modastack root", ok=False,
        detail="; ".join(parts),
        hint=" ".join(hints))


def _check_services() -> list[CheckResult]:
    """Run service validation — native credentials, Venn, MCP servers."""
    root = bound_root()
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
    if bound_root() is None:
        return CheckResult("Workflows", ok=False,
                           detail="no project detected",
                           hint="Run from inside a Modastack installation")
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


def _check_bubble_auth() -> CheckResult:
    """Check bubble identity and auth configuration.

    Shows the instance's bubble_id (public, safe) and confirms a bubble key
    is present. Warns on remote+cleartext event server URLs (the bubble key
    would transit in the clear at mint time) and on a missing key when the
    instance appears to be running.
    """
    root = bound_root()
    if not root:
        return CheckResult("Bubble auth", ok=True, detail="no project detected")

    from modastack.config import Config, load_bubble_state

    bubble = load_bubble_state(root)
    bubble_id = bubble.get("bubble_id", "")
    has_key = bool(bubble.get("bubble_key"))

    try:
        cfg = Config.load(root)
    except Exception:
        cfg = None

    # Check for remote + non-TLS event server URL — the bubble key would
    # transit cleartext at registration (MINT).
    es_url = cfg.event_server_url if cfg else ""
    if es_url:
        from modastack.events.server import _is_loopback_or_tls
        if not _is_loopback_or_tls(es_url):
            return CheckResult(
                "Bubble auth", ok=False,
                detail=f"event server URL is remote + cleartext ({es_url})",
                hint="The bubble key transits at mint time — use https:// "
                     "or a loopback event server to protect it")

    if not bubble_id:
        # No bubble yet — might be fine if the instance hasn't started.
        from modastack import paths
        pid_file = paths.state_dir(root) / "event-server.pid"
        if pid_file.exists():
            return CheckResult(
                "Bubble auth", ok=False,
                detail="no bubble credential but event server appears running",
                hint="The agent would mint a fresh/orphan bubble on next "
                     "registration — run `modastack restart` to re-establish")
        return CheckResult("Bubble auth", ok=True,
                           detail="no bubble yet (instance not started)")

    if not has_key:
        return CheckResult(
            "Bubble auth", ok=False,
            detail=f"bubble_id {bubble_id} present but bubble_key missing",
            hint="The bubble credential is incomplete — run `modastack restart` "
                 "to re-mint")

    return CheckResult("Bubble auth", ok=True,
                       detail=f"bubble {bubble_id[:20]}… key present")


def _check_event_server() -> CheckResult:
    """Probe the event server /health endpoint."""
    from modastack.config import Config
    from modastack.events.server import health

    root = bound_root()
    if root:
        try:
            cfg = Config.load(root)
            # Deployment state is per-session (state/deployments/<session>.json);
            # any registered session means the remote server is in use.
            from modastack import paths
            deployments_dir = paths.state_path(root) / "deployments"
            registered = (deployments_dir.is_dir()
                          and any(deployments_dir.glob("*.json")))
            if cfg.event_server_url and registered:
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
    root = bound_root()
    if not root:
        return CheckResult("Recent events", ok=False, detail="no project detected")
    from modastack import paths
    state_dir = paths.state_path(root)
    event_files = list(state_dir.glob("events-*.jsonl"))
    if not event_files:
        return CheckResult("Recent events", ok=True, detail="no events yet")
    total = sum(len(f.read_text().strip().splitlines()) for f in event_files)
    return CheckResult("Recent events", ok=True,
                       detail=f"{total} events logged across {len(event_files)} file(s)")


def _check_memory() -> CheckResult:
    """Check agent decision logs — flag agents with empty current-state blocks."""
    root = bound_root()
    if not root:
        return CheckResult("Decision log", ok=True, detail="no project detected")
    from modastack import paths
    memory_root = paths.state_path(root) / "memory"
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
