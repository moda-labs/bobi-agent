"""System health checks — manager, event server, package, workflows."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from bobi.paths import bound_root


@dataclass
class CheckResult:
    """Outcome of a single health check."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # Only meaningful when ok is False: a required failure is a real problem;
    # a non-required failure is a warning (e.g. an optional service that's
    # unconfigured but doesn't block start). Defaults True so all
    # existing health checks keep counting as hard failures.
    required: bool = True
    # Set when the failure is specifically the AppArmor userns sandbox block,
    # so callers can offer the targeted fix.
    sandbox_error: bool = False


def run_doctor() -> list[CheckResult]:
    logging.getLogger("bobi").setLevel(logging.WARNING)

    results = []

    results.append(_check_claude_cli())
    results.append(_check_claude_auth())
    results.append(_check_local_config())
    results.append(_check_runtime_layout())
    results.append(_check_runtime_write_policy())
    results.append(_check_install_integrity())
    results.append(_check_bobi_install_integrity())
    results.extend(_check_package_requires())
    results.extend(_check_host_caps())
    results.extend(_check_services())
    results.append(_check_workflows())
    results.append(_check_bubble_auth())
    results.append(_check_event_server())
    slack_socket = _check_slack_socket_mode()
    if slack_socket:
        results.append(slack_socket)
    results.append(_check_ingress_reachability())
    results.append(_check_recent_events())
    results.append(_check_long_term_memory())

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
    """Flag edits to the installed run/package image.

    The installed copy is frozen — regenerated verbatim by
    `bobi agents install` — so hand-edits are silently lost on the next install.
    Compare on-disk files against the hashes recorded at install time.
    """
    import hashlib
    import json

    root = bound_root()
    if not root:
        return CheckResult("Installed team", ok=True, detail="no runtime selected")
    from bobi import paths
    dest = paths.package_dir(root)
    manifest_path = dest / "install-manifest.json"
    if not manifest_path.exists():
        return CheckResult("Installed team", ok=True,
                           detail="no install manifest")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckResult("Installed team", ok=False,
                           detail="unreadable install manifest",
                           hint="Re-run `bobi agents install ... --name <agent>`")
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
            hint="Edits to run/package/ are lost on reinstall — edit the "
                 "source and re-run `bobi agents install ... --name <agent>`")
    return CheckResult("Installed team", ok=True,
                       detail=f"{manifest.get('agent', '?')} (frozen, clean)")


def _check_runtime_write_policy() -> CheckResult:
    root = bound_root()
    if not root:
        return CheckResult("Runtime write policy", ok=True,
                           detail="no runtime selected")
    from bobi.runtime_guard import check_runtime_write_policy

    result = check_runtime_write_policy(root)
    if result.ok:
        return CheckResult("Runtime write policy", ok=True, detail=result.detail)
    return CheckResult(
        "Runtime write policy",
        ok=False,
        detail=result.detail,
        hint=(
            "Runtime package images should be read-only. Re-run "
            "`bobi agents install ... --name <agent>`; same-UID chmod can "
            "bypass this guardrail, so use read-only mounts or split ownership "
            "for a hard boundary."
        ),
    )


def _check_bobi_install_integrity() -> CheckResult:
    from bobi.runtime_guard import check_bobi_distribution_integrity

    result = check_bobi_distribution_integrity()
    if result.ok:
        return CheckResult("Bobi install", ok=True, detail=result.detail)
    return CheckResult(
        "Bobi install",
        ok=False,
        detail=result.detail,
        hint=(
            "Reinstall or upgrade Bobi, and move any desired framework changes "
            "into a source PR instead of editing the installed package."
        ),
    )


def _check_package_requires() -> list[CheckResult]:
    """Check host-level dependencies declared in agent.yaml requires: block."""
    from bobi.config import Config, run_requires_checks

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


def _check_host_caps() -> list[CheckResult]:
    """Verify host capabilities a dependency declared via `host:` (#428 Stage 3).

    A host capability (a kernel sysctl, a device) is provisioned on the host, not
    baked into the image — the in-container agent cannot grant it. Doctor reports
    whether each is satisfied on THIS host; where the knob is absent (older kernel,
    non-Linux) the capability simply doesn't apply and the check passes.
    """
    from bobi.config import Config
    from bobi.host_caps import parse_host_caps

    root = bound_root()
    if not root:
        return []
    try:
        cfg = Config.load(root)
    except Exception:
        return []
    if not cfg.host:
        return []
    return [cap.check() for cap in parse_host_caps(cfg.host)]


def _check_local_config() -> CheckResult:
    from bobi.config import _project_config_path
    root = bound_root()
    if not root:
        return CheckResult("Package config", ok=False,
                           detail="no runtime selected",
                           hint="Select a Bobi Agent with `bobi agent <name> doctor`")
    config_path = _project_config_path(root)
    if config_path.exists():
        return CheckResult("Package config", ok=True, detail=str(config_path))
    return CheckResult("Package config", ok=False,
                       detail=f"missing {config_path}",
                       hint="Install a package with `bobi agents install ... --name <agent>`")


def _check_runtime_layout() -> CheckResult:
    """Validate the selected runtime root uses the canonical layout."""
    root = bound_root()
    if not root:
        return CheckResult("Runtime layout", ok=False,
                           detail="no Bobi Agent runtime selected",
                           hint="Run `bobi agent <name> doctor`")
    from bobi import paths
    expected = {
        "package/agent.yaml": paths.agent_yaml_path(root),
        "state/": paths.state_path(root),
        "workspace/": paths.workspace_dir(root),
    }
    missing = [label for label, path in expected.items()
               if not (path.is_dir() if label.endswith("/") else path.is_file())]
    if missing:
        return CheckResult(
            "Runtime layout", ok=False,
            detail=f"{root} missing {', '.join(missing)}",
            hint="Reinstall with `bobi agents install <source> --name <agent>`")
    try:
        slot = paths.agent_name_for_root(root)
    except Exception:
        slot = root.name
    return CheckResult("Runtime layout", ok=True,
                       detail=f"{slot}: {root}")


def _check_services() -> list[CheckResult]:
    """Run service validation — native credentials, Venn, MCP servers."""
    root = bound_root()
    if not root:
        return []
    try:
        from bobi.validate import validate_config
        result = validate_config(root)
        return [
            CheckResult(c.name, ok=c.ok, detail=c.detail, hint=c.hint,
                        required=c.required)
            for c in result.checks
        ]
    except Exception as e:
        return [CheckResult("Services", ok=False, detail=f"validation error: {e}")]


def _check_workflows() -> CheckResult:
    if bound_root() is None:
        return CheckResult("Workflows", ok=False,
                           detail="no runtime selected",
                           hint="Run `bobi agent <name> doctor`")
    try:
        from bobi.workflow.triggers import WorkflowDispatcher
        d = WorkflowDispatcher()
        d.load_all_workflows()
        names = [wf.name for wf, _ in d.workflows]
        if not names:
            return CheckResult("Workflows", ok=False,
                               detail="none found",
                               hint="Add workflows to the agent package source and reinstall")
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
        return CheckResult("Bubble auth", ok=True, detail="no runtime selected")

    from bobi.config import Config, load_bubble_state

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
        from bobi.events.server import _is_loopback_or_tls
        if not _is_loopback_or_tls(es_url):
            return CheckResult(
                "Bubble auth", ok=False,
                detail=f"event server URL is remote + cleartext ({es_url})",
                hint="The bubble key transits at mint time — use https:// "
                     "or a loopback event server to protect it")

    if not bubble_id:
        # No bubble yet — might be fine if the instance hasn't started.
        from bobi import paths
        pid_file = paths.state_dir(root) / "event-server.pid"
        if pid_file.exists():
            return CheckResult(
                "Bubble auth", ok=False,
                detail="no bubble credential but event server appears running",
                hint="The agent would mint a fresh/orphan bubble on next "
                     "registration — run `bobi agent <name> restart` to re-establish")
        return CheckResult("Bubble auth", ok=True,
                           detail="no bubble yet (instance not started)")

    if not has_key:
        return CheckResult(
            "Bubble auth", ok=False,
            detail=f"bubble_id {bubble_id} present but bubble_key missing",
            hint="The bubble credential is incomplete — run "
                 "`bobi agent <name> restart` to re-mint")

    return CheckResult("Bubble auth", ok=True,
                       detail=f"bubble {bubble_id[:20]}… key present")


def _check_event_server() -> CheckResult:
    """Probe the event server /health endpoint."""
    from bobi.config import Config
    from bobi.events.server import (
        NodeRuntimePrerequisiteError,
        _is_local_url,
        health,
        resolve_node_runtime,
    )

    root = bound_root()
    needs_local_node = True
    if root:
        try:
            cfg = Config.load(root)
            # Deployment state is per-session (state/deployments/<session>.json);
            # any registered session means the remote server is in use.
            from bobi import paths
            deployments_dir = paths.state_path(root) / "deployments"
            registered = (deployments_dir.is_dir()
                          and any(deployments_dir.glob("*.json")))
            if cfg.event_server_url and registered:
                return CheckResult("Event server", ok=True,
                                   detail=f"remote ({cfg.event_server_url})")
            if cfg.event_server_url and not _is_local_url(cfg.event_server_url):
                needs_local_node = False
        except FileNotFoundError:
            pass

    url = "http://localhost:8080"
    if health(url):
        return CheckResult("Event server", ok=True, detail=url)
    if needs_local_node:
        try:
            resolve_node_runtime()
        except NodeRuntimePrerequisiteError as exc:
            return CheckResult(
                "Event server",
                ok=False,
                detail=str(exc),
                hint="Install Node.js 20+ and ensure `node` is on PATH, then run doctor again.",
            )
    return CheckResult("Event server", ok=False,
                       detail="not running",
                       hint="`bobi agent <name> event-server start` or `bobi agent <name> start` will auto-launch")


def _check_slack_socket_mode() -> CheckResult | None:
    """Report the configured Slack app's local Socket Mode connection."""
    root = bound_root()
    if not root:
        return None

    from bobi.config import Config
    from bobi.events.server import _slack_app_id, _slack_auth_info, health

    try:
        cfg = Config.load(root)
    except FileNotFoundError:
        return None

    app_token = str(cfg.credential("slack", "app_token") or "").strip()
    if not app_token:
        return None

    url = cfg.event_server_url or "http://localhost:8080"
    bot_token = str(cfg.credential("slack", "bot_token") or "").strip()

    def safe_health_text(value: object) -> str:
        """Sanitize untrusted health fields before terminal output."""
        text = str(value or "")
        for secret in (app_token, bot_token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return "".join(char if char.isprintable() else "?" for char in text)

    server_health = health(url)
    if not server_health:
        return CheckResult(
            "Slack Socket Mode",
            ok=False,
            detail=f"event server health is unavailable at {url}",
            hint="Start or reconnect the local event server, then run doctor again.",
            required=False,
        )
    if server_health.get("mode") != "local":
        mode = safe_health_text(server_health.get("mode") or "unknown")
        return CheckResult(
            "Slack Socket Mode",
            ok=False,
            detail=f"app token is ineffective on remote event server {url} (mode {mode})",
            hint="Use the local Node event server for Socket Mode, or remove "
                 "SLACK_APP_TOKEN and configure public webhook ingress.",
            required=False,
        )

    entries = server_health.get("slack_socket")
    if not isinstance(entries, list):
        return CheckResult(
            "Slack Socket Mode",
            ok=False,
            detail="local event server has no Slack Socket Mode health data",
            hint="Socket Mode is unsupported or the app is not registered yet; "
                 "restart the agent after configuring SLACK_APP_TOKEN.",
            required=False,
        )

    _team_id, bot_id, _bot_user_id = _slack_auth_info(bot_token)
    app_id = _slack_app_id(bot_token, bot_id)
    if not app_id:
        return CheckResult(
            "Slack Socket Mode",
            ok=False,
            detail="configured Slack app identity is unavailable",
            hint="Check SLACK_BOT_TOKEN, then restart the agent and run doctor again.",
            required=False,
        )

    entry = next(
        (
            item for item in entries
            if isinstance(item, dict)
            and str(item.get("application_id") or "") == app_id
        ),
        None,
    )
    if entry is None:
        return CheckResult(
            "Slack Socket Mode",
            ok=False,
            detail=f"configured Slack app {app_id} is not registered",
            hint="Restart the agent to register its app token with the local event server.",
            required=False,
        )

    raw_state = str(entry.get("state") or "unknown")
    state = safe_health_text(raw_state)
    if raw_state == "connected":
        return CheckResult(
            "Slack Socket Mode",
            ok=True,
            detail=f"{app_id} connected",
            required=False,
        )

    detail = f"{app_id} {state}"
    fatal_reason = safe_health_text(entry.get("fatal_reason"))
    if fatal_reason:
        detail += f": {fatal_reason}"
    if raw_state == "fatal":
        hint = "Fix the Slack app token or Socket Mode settings, then restart the agent."
    elif raw_state in {"connecting", "reconnecting", "backoff"}:
        hint = "The socket is retrying; check network access and run doctor again."
    else:
        hint = "Restart the agent and run doctor again to verify the socket connection."
    return CheckResult(
        "Slack Socket Mode",
        ok=False,
        detail=detail,
        hint=hint,
        required=False,
    )


def _check_ingress_reachability() -> CheckResult:
    """Warn when external webhook sources point at local-only ingress."""
    root = bound_root()
    if not root:
        return CheckResult("Ingress reachability", ok=True,
                           detail="no runtime selected")
    try:
        from bobi.ingress import check_ingress_reachability

        warning = check_ingress_reachability(root)
    except FileNotFoundError:
        return CheckResult("Ingress reachability", ok=True,
                           detail="no agent config")
    except Exception as exc:
        return CheckResult("Ingress reachability", ok=True,
                           detail=f"skipped: {exc}")
    if not warning:
        return CheckResult("Ingress reachability", ok=True,
                           detail="inbound event URL is reachable")
    return CheckResult("Ingress reachability", ok=False,
                       detail=warning.detail, hint=warning.hint,
                       required=False)


def _check_recent_events() -> CheckResult:
    root = bound_root()
    if not root:
        return CheckResult("Recent events", ok=False, detail="no runtime selected")
    from bobi import paths
    state_dir = paths.state_path(root)
    event_files = list(state_dir.glob("events-*.jsonl"))
    if not event_files:
        return CheckResult("Recent events", ok=True, detail="no events yet")
    total = sum(len(f.read_text().strip().splitlines()) for f in event_files)
    return CheckResult("Recent events", ok=True,
                       detail=f"{total} events logged across {len(event_files)} file(s)")


def _check_long_term_memory() -> CheckResult:
    """Check long_term_memory.md (#456): present, under cap, and not foreign-written.

    The sleep cycle is the single writer. A soft single-writer guard (Q5): the
    sleep cycle rewrites long_term_memory.md and advances long_term_memory_cursor
    together, so a memory file whose mtime is newer than the cursor file's mtime
    is a write not attributable to the last sleep-cycle run.
    """
    from bobi.memory import MAX_MEMORY_CHARS
    root = bound_root()
    if not root:
        return CheckResult("Long-term memory", ok=True, detail="no runtime selected")
    from bobi import paths
    paths.migrate_long_term_memory_state(root)
    memory = paths.long_term_memory_path(root)
    if not memory.is_file():
        monitor_state = paths.state_path(root) / "monitor_state.json"
        cursor = paths.long_term_memory_cursor_path(root)
        try:
            import json
            state = json.loads(monitor_state.read_text()) if monitor_state.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        sleep_cycle_state = state.get("sleep-cycle") if isinstance(state, dict) else None
        if (
            isinstance(sleep_cycle_state, dict)
            and sleep_cycle_state.get("last_spawn")
            and not cursor.is_file()
        ):
            return CheckResult(
                "Long-term memory", ok=False,
                detail=("sleep-cycle has spawned but long_term_memory.md and "
                        "long_term_memory_cursor are absent"),
                hint="Check manager.log for sleep-cycle launch or result parsing errors")
        backlog = 0
        try:
            from bobi import history
            backlog = len(history.messages_since(0, limit=101))
        except Exception:
            backlog = 0
        if backlog > 100:
            cursor = paths.long_term_memory_cursor_path(root)
            cursor_detail = (
                "no long_term_memory_cursor"
                if not cursor.is_file()
                else "stale long_term_memory_cursor"
            )
            return CheckResult(
                "Long-term memory", ok=False,
                detail=(f"no long_term_memory.md yet, but at least {backlog} transcript "
                        f"messages are pending and {cursor_detail}"),
                hint=("The sleep cycle appears stalled; check monitor.error "
                      "events and manager.log"))
        return CheckResult("Long-term memory", ok=True,
                           detail="no long_term_memory.md yet (sleep cycle seeds it on first run)")

    try:
        size = len(memory.read_text())
    except OSError as e:
        return CheckResult("Long-term memory", ok=False,
                           detail=f"unreadable long_term_memory.md: {e}")

    if size > MAX_MEMORY_CHARS:
        return CheckResult(
            "Long-term memory", ok=False,
            detail=f"long_term_memory.md is {size} chars (over {MAX_MEMORY_CHARS} cap)",
            hint="The sleep cycle should keep long_term_memory.md under cap - check sleep cycle runs")

    cursor = paths.long_term_memory_cursor_path(root)
    if cursor.is_file():
        try:
            if memory.stat().st_mtime > cursor.stat().st_mtime + 1.0:
                return CheckResult(
                    "Long-term memory", ok=False,
                    detail=("long_term_memory.md modified after the last sleep-cycle "
                            "cursor advance - a non-sleep-cycle write may have occurred"),
                    hint="Only the sleep cycle should write long_term_memory.md (single-writer)")
        except OSError:
            pass

    return CheckResult(
        "Long-term memory", ok=True,
        detail=f"long_term_memory.md present ({size} chars, under {MAX_MEMORY_CHARS} cap)")


def _check_policy() -> CheckResult:
    """Deprecated compatibility wrapper for one release."""
    return _check_long_term_memory()
