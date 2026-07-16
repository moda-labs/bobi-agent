"""Startup config validation.

Runs before the agent session starts to catch misconfigurations early:
role exists, credentials present, MCP servers connect, Venn services
are logged in.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bobi.env import child_agent_env
from bobi.mcp_handshake import preflight_timeout

log = logging.getLogger(__name__)

# Stdio MCP servers spawn a subprocess and run their MCP `initialize` handshake
# (~1–2s), so a single status read catches them mid-spawn in the `pending`
# window and false-fails them (MDS-63). Poll until each target server leaves
# `pending`, bounded by BOBI_MCP_PREFLIGHT_TIMEOUT (default 10s).
MCP_PROBE_POLL_INTERVAL = 0.5


def _mcp_probe_max_polls() -> int:
    timeout = preflight_timeout()
    if MCP_PROBE_POLL_INTERVAL <= 0:
        return math.ceil(timeout)
    return math.ceil(timeout / MCP_PROBE_POLL_INTERVAL)


def supports_unicode(stream=None) -> bool:
    """True if *stream* (default stdout) can encode the status glyphs.

    Unicode-stripped terminals (ASCII/POSIX locales, redirected pipes with
    no declared encoding) fall back to bracketed text markers via
    ``status_glyph`` so preflight output never raises or mojibakes.
    """
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None)
    if not enc:
        return False
    try:
        "✓⚠✗".encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def status_glyph(ok: bool, required: bool, *, unicode: bool | None = None) -> str:
    """Status marker for a check.

    ``✓`` ok, ``✗`` blocking (required) failure, ``⚠`` non-blocking warning.
    Falls back to ``[OK]`` / ``[ERROR]`` / ``[WARN]`` when the terminal can't
    encode unicode. ``required`` is only consulted when ``ok`` is False.
    Pass ``unicode`` explicitly to avoid re-probing stdout per row.
    """
    if unicode is None:
        unicode = supports_unicode()
    if ok:
        return "✓" if unicode else "[OK]"
    if required:
        return "✗" if unicode else "[ERROR]"
    return "⚠" if unicode else "[WARN]"


@dataclass
class ValidationResult:
    ok: bool
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def errors(self) -> list[CheckResult]:
        """All failed checks — both blocking (required) and warnings."""
        return [c for c in self.checks if not c.ok]

    def format(self) -> str:
        unicode = supports_unicode()
        lines = []
        for c in self.checks:
            icon = status_glyph(c.ok, c.required, unicode=unicode)
            lines.append(f"  {icon} {c.name:30} {c.detail}")
            if not c.ok and c.hint:
                lines.append(f"    → {c.hint}")
        return "\n".join(lines)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    # Only meaningful when ok is False: a required failure blocks startup; a
    # non-required failure is a warning and the agent starts degraded.
    # Defaults True so existing call sites (entry point, MCP) keep blocking.
    required: bool = True


def validate_config(project_path: Path) -> ValidationResult:
    """Run all startup validation checks."""
    from bobi.config import Config

    cfg = Config.load(project_path)
    checks: list[CheckResult] = []

    checks.append(_check_entry_point(cfg, project_path))
    checks.extend(_check_brain(cfg))
    checks.extend(_check_roles(cfg, project_path))
    checks.extend(_check_effort(cfg))
    checks.extend(_check_workflow_effort(cfg, project_path))
    checks.extend(_check_monitor_relevance(project_path))
    checks.extend(_check_service_credentials(cfg))
    checks.extend(_check_venn_services(cfg))
    checks.extend(_check_mcp_servers(cfg, project_path))

    return ValidationResult(
        # Block only on required failures; non-required failures are warnings
        # and the agent starts degraded.
        ok=not any((not c.ok) and c.required for c in checks),
        checks=checks,
    )


def _check_entry_point(cfg, project_path: Path) -> CheckResult:
    if not cfg.entry_point:
        return CheckResult("entry_point", ok=True, detail="not set, defaulting to manager")

    from bobi import paths
    installed_roles = paths.roles_dir(project_path)
    role_dir = installed_roles / cfg.entry_point
    if role_dir.is_dir():
        return CheckResult("entry_point", ok=True, detail=cfg.entry_point)

    if installed_roles.is_dir():
        return CheckResult(
            "entry_point", ok=False,
            detail=f"role '{cfg.entry_point}' not found",
            hint=f"Available roles in {installed_roles}",
        )

    return CheckResult("entry_point", ok=True, detail=cfg.entry_point)


def _check_brain(cfg) -> list[CheckResult]:
    """Validate the `brain:` block where a bad value fails mid-session (#655).

    Unknown kinds already fail loud at session construction (``get_brain``
    raises), so validate-time checks cover the gateway configuration (#789):
    without a resolvable `base_url` every session would 404/hang at its first
    turn - or worse, dial the real vendor endpoint carrying gateway
    credentials. A `${VAR}` that didn't resolve interpolates to "" and lands
    here too. The auth token (``ANTHROPIC_AUTH_TOKEN``) is deliberately not
    required - Ollama serves unauthenticated.
    """
    from bobi.brain import (
        BRAIN_KIND_ALIASES,
        DEFAULT_BRAIN,
        GATEWAY_ENGINES,
        normalize_brain_kind,
    )

    results: list[CheckResult] = []
    kind = cfg.brain_kind
    # An empty kind is the framework default engine, so a kind-less
    # `brain: {base_url: ...}` is a claude-engine gateway team.
    engine = normalize_brain_kind(kind) or DEFAULT_BRAIN
    if kind in BRAIN_KIND_ALIASES:
        results.append(CheckResult(
            "brain.kind", ok=True,
            detail=f"kind: {kind} is a deprecated spelling",
            hint=f"use kind: {engine} with brain.base_url instead",
        ))
    if not cfg.brain_is_gateway:
        return results
    if engine not in GATEWAY_ENGINES:
        # base_url on a non-engine kind (stub) is ignored by the pin sites;
        # say so rather than let the config imply a gateway that never dials.
        results.append(CheckResult(
            "brain.gateway", ok=True,
            detail=f"brain.base_url is ignored for kind: {kind}",
            hint="gateway mode applies to kind: claude or kind: codex",
        ))
        return results
    name = "brain.gateway" if engine == "claude" else "brain.gateway_openai"
    if not cfg.brain_base_url:
        endpoint = "OpenAI-compatible /v1 endpoint" \
            if engine == "codex" else "gateway endpoint"
        results.append(CheckResult(
            name, ok=False,
            detail="a gateway team requires a non-empty brain.base_url",
            hint=f"set brain.base_url to the {endpoint} "
                 "(e.g. http://localhost:4000 or ${LLM_GATEWAY_URL} with the "
                 "variable in the runtime .env)",
        ))
        return results
    if engine == "codex" and cfg.brain_wire_api not in ("chat", "responses"):
        results.append(CheckResult(
            name, ok=False,
            detail="a codex gateway team requires brain.wire_api to be chat or responses",
            hint="remove brain.wire_api for the responses default, or set it to responses",
        ))
        return results
    if engine == "codex" and str((cfg.brain or {}).get("wire_api", "")) == "chat":
        results.append(CheckResult(
            name, ok=False,
            detail="brain.wire_api: chat is deprecated for codex gateways",
            hint="front chat-only OpenAI-compatible gateways with LiteLLM's "
                 "Responses API translation, or use kind: claude + "
                 "brain.base_url for Anthropic-compatible gateways",
            required=False,
        ))
        return results
    results.append(CheckResult(name, ok=True, detail=cfg.brain_base_url))
    return results


# Roles the runtime uses without a roles/ prompt directory. Monitor checks
# always launch as role "monitor" (bobi/subagent.py run_check_blocking), so
# roles.monitor.* is meaningful in every pack.
_BUILTIN_ROLES = {"monitor"}

# The union of reasoning-effort values across the known brains (#778), the
# fallback when the configured brain does not declare its own accepted set
# (BrainCapabilities.efforts). Effort is pass-through like model, so an
# unknown value is only a warning here (the vendor CLIs grow new tiers).
_KNOWN_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def _accepted_efforts(cfg) -> set[str]:
    """The effort values the configured brain accepts, else the union.

    Each engine declares its accepted set on ``capabilities.efforts``
    (claude: low..max; codex: none..xhigh), so a cross-vendor value like
    ``kind: codex`` + ``effort: max`` warns instead of hiding in the union.
    The engine's set applies in gateway mode too - the engine CLI is what
    parses the value (#789). Only an unknown/undeclared brain (stub) falls
    back to the union.
    """
    from bobi.brain import DEFAULT_BRAIN, get_brain, normalize_brain_kind

    try:
        engine = normalize_brain_kind(cfg.brain_kind) or DEFAULT_BRAIN
        efforts = get_brain(engine).capabilities.efforts
    except Exception:
        efforts = frozenset()
    return set(efforts) or set(_KNOWN_EFFORTS)


def _check_effort(cfg) -> list[CheckResult]:
    """Warn on unrecognized `effort:` values (#778).

    A wrong value never fails at validate: codex 400s at the first turn,
    and the claude CLI warns and silently runs on its default effort - the
    worst failure mode, because the run LOOKS fine. So surface likely typos
    here. Warnings, never blocking: the accepted set is a snapshot of
    today's vendor tiers, not an allowlist.
    """
    # Presence-based, not truthiness: `effort: no` (YAML False) or `effort: 0`
    # is a malformed value the runtime silently drops (str(value or "") -> ""),
    # exactly what this check must catch. None/"" mean "unset" and pass.
    def _configured(entry: dict) -> object | None:
        value = entry.get("effort")
        return None if value is None or value == "" else value

    found: list[tuple[str, object]] = []
    if isinstance(cfg.brain, dict) and _configured(cfg.brain) is not None:
        found.append(("brain.effort", cfg.brain["effort"]))
    if isinstance(cfg.roles, dict):
        for name, entry in cfg.roles.items():
            if isinstance(entry, dict) and _configured(entry) is not None:
                found.append((f"roles.{name}.effort", entry["effort"]))

    accepted = _accepted_efforts(cfg)
    brain_label = cfg.brain_kind or "claude"
    checks = []
    for label, value in found:
        if not isinstance(value, str) or value not in accepted:
            checks.append(CheckResult(
                label, ok=False, required=False,
                detail=f"effort {value!r} is not accepted by the "
                       f"{brain_label} brain",
                hint=f"accepted values: {', '.join(sorted(accepted))}",
            ))
    return checks


def _check_workflow_effort(cfg, project_path: Path) -> list[CheckResult]:
    """Warn on unrecognized step-level `effort:` in workflow YAMLs (#778).

    The step field rides the same pass-through contract as the config-level
    values `_check_effort` covers, and fails the same silent way at runtime,
    so it gets the same typo warning. Malformed workflow files are skipped -
    they surface through the workflow loader's own paths.
    """
    import yaml as _yaml

    from bobi import paths

    wf_dir = paths.workflows_dir(project_path)
    if not wf_dir.is_dir():
        return []

    accepted = _accepted_efforts(cfg)
    brain_label = cfg.brain_kind or "claude"
    checks = []
    for wf_path in sorted(wf_dir.glob("*.yaml")):
        try:
            raw = _yaml.safe_load(wf_path.read_text()) or {}
        except Exception:
            continue
        for step in raw.get("steps", []) if isinstance(raw, dict) else []:
            if not isinstance(step, dict):
                continue
            value = step.get("effort")
            if value is None or value == "":
                continue
            if not isinstance(value, str) or value not in accepted:
                label = f"{wf_path.name}:{step.get('name', '?')}.effort"
                checks.append(CheckResult(
                    label, ok=False, required=False,
                    detail=f"effort {value!r} is not accepted by the "
                           f"{brain_label} brain",
                    hint=f"accepted values: {', '.join(sorted(accepted))}",
                ))
    return checks


def _check_roles(cfg, project_path: Path) -> list[CheckResult]:
    """Validate the `roles:` mapping (#617).

    A misconfigured entry fails silently at runtime (the agent just runs on
    the default model), so surface shape errors and unknown role names here
    as warnings.
    """
    if not isinstance(cfg.roles, dict) or not cfg.roles:
        return []

    from bobi import paths
    installed_roles = paths.roles_dir(project_path)
    known = set(_BUILTIN_ROLES)
    if installed_roles.is_dir():
        known.update(p.name for p in installed_roles.iterdir() if p.is_dir())

    checks = []
    for name, entry in cfg.roles.items():
        if not isinstance(entry, dict):
            checks.append(CheckResult(
                f"roles.{name}", ok=False, required=False,
                detail=f"must be a mapping, got {type(entry).__name__}",
                hint=f"write `roles: {{{name}: {{model: {entry}}}}}`",
            ))
            continue
        # Only warn about unknown names when the pack ships role dirs to
        # check against; a dirless pack can't distinguish typo from intent.
        if installed_roles.is_dir() and name not in known:
            checks.append(CheckResult(
                f"roles.{name}", ok=False, required=False,
                detail="unknown role name; its model override will never apply",
                hint=f"known roles: {', '.join(sorted(known))}",
            ))
    return checks


def _check_monitor_relevance(project_path: Path) -> list[CheckResult]:
    """Warn when `relevance:` is set on a monitor flavor it cannot gate (#630).

    The relevance gate only applies to mechanical detectors (`command:` or
    `check:`); on flavors without one (notify, description-only, curator-only)
    the key is silently ignored at runtime, so surface it here as a warning.
    """
    try:
        from bobi.monitors.registry import MonitorRegistry
        monitors = MonitorRegistry.load(project_path=project_path).effective_monitors()
    except Exception:
        return []  # monitor config problems surface through their own paths

    checks = []
    for m in monitors:
        # Monitor.gated is the same predicate the scheduler routes on, so
        # validate can never drift from the runtime.
        if not m.relevance or m.gated:
            continue
        checks.append(CheckResult(
            f"monitors.{m.name}", ok=False, required=False,
            detail="relevance: is ignored on this flavor",
            hint="the relevance gate needs a mechanical detector "
                 "(command: or check:, e.g. tool_poll/venn_poll)",
        ))
    return checks


def _check_service_credentials(cfg) -> list[CheckResult]:
    """Validate declared credentials for all services with registered adapters.

    Every declared credential key must interpolate to a non-empty value.
    Services with no declared credentials (e.g. github — auth rides on `gh`)
    pass automatically.
    """
    from bobi.events.adapters import is_registered

    checks = []
    for svc in cfg.services:
        if not is_registered(svc.name):
            continue
        if not svc.credentials:
            checks.append(CheckResult(svc.name, ok=True, detail="native"))
            continue
        missing = [k for k, v in svc.credentials.items() if not v]
        if missing:
            keys_str = ", ".join(missing)
            checks.append(CheckResult(
                svc.name, ok=False,
                detail=f"native — missing {keys_str}",
                hint=f"Set credentials for {svc.name} in agent.yaml",
                required=svc.required,
            ))
        else:
            checks.append(CheckResult(svc.name, ok=True, detail="native"))
    return checks


def _check_venn_services(cfg) -> list[CheckResult]:
    venn_services = cfg.venn_services
    if not venn_services:
        return []

    if not cfg.venn_api_key:
        return [
            CheckResult(
                s.name, ok=False,
                detail="venn — no API key",
                hint="Set venn_api_key in agent.yaml or VENN_API_KEY in environment",
                required=s.required,
            )
            for s in venn_services
        ]

    from bobi.venn import check_services
    result = check_services(cfg.venn_api_key, [s.name for s in venn_services])

    # check_services returns only name strings, so map back to the declaring
    # ServiceConfig(s) to carry each service's `required` flag. A name can be
    # declared more than once; treat a failure as blocking if ANY declaration
    # marked it required (fail safe toward blocking), and default to blocking
    # for an unrecognized name.
    def _required_for(name: str) -> bool:
        decls = [s for s in venn_services if s.name == name]
        return any(s.required for s in decls) if decls else True

    checks = []
    for name in result.connected:
        checks.append(CheckResult(name, ok=True, detail="venn"))
    for name in result.missing:
        checks.append(CheckResult(
            name, ok=False,
            detail="venn — not connected",
            hint="Connect at venn.ai, then restart",
            required=_required_for(name),
        ))
    return checks


def _is_bare_command(command) -> bool:
    """True if *command* is a bare executable name resolved via PATH.

    A bare name (no directory component, not absolute) is the MDS-64 footgun:
    it resolves in the rich foreground shell at preflight but historically not
    under the daemon's stripped PATH at agent spawn. With agent_spawn_env() the
    runtime now resolves it too, so this is a non-blocking ⚠ heads-up, not a
    failure.
    """
    if not isinstance(command, str) or not command:
        return False
    return not os.path.isabs(command) and os.path.basename(command) == command


def _check_mcp_servers(cfg, project_path: Path) -> list[CheckResult]:
    if not cfg.mcp_servers:
        return []

    checks = []
    probe_names = []
    for name, server_cfg in cfg.mcp_servers.items():
        server_type = server_cfg.get("type", "stdio")

        if server_type in ("http", "sse"):
            if not server_cfg.get("url"):
                checks.append(CheckResult(
                    name, ok=False,
                    detail="mcp — missing url",
                    hint=f"Set mcp_servers.{name}.url in agent.yaml",
                ))
                continue
        elif server_type == "stdio":
            command = server_cfg.get("command")
            if not command:
                checks.append(CheckResult(
                    name, ok=False,
                    detail="mcp — missing command",
                    hint=f"Set mcp_servers.{name}.command in agent.yaml",
                ))
                continue
            # D-64c: non-blocking warning on bare-name stdio commands.
            if _is_bare_command(command):
                checks.append(CheckResult(
                    name, ok=False,
                    detail=f"mcp — '{command}' is a bare command name (resolved via PATH)",
                    hint="Agents resolve it via ~/.local/bin; use an absolute "
                         "path to be PATH-independent",
                    required=False,
                ))

        probe_names.append(name)

    # D-63b: one connect()/poll loop judges every server, bounding latency.
    if probe_names:
        checks.extend(_probe_mcp_servers(probe_names, cfg.mcp_servers, project_path))

    return checks


def _probe_mcp_servers(
    names: list[str],
    all_servers: dict[str, dict],
    project_path: Path,
) -> list[CheckResult]:
    """Connect once via the Claude SDK and judge every named server."""
    try:
        return asyncio.run(_async_probe_mcp(names, all_servers, project_path))
    except asyncio.TimeoutError:
        timeout = preflight_timeout()
        return [
            CheckResult(
                name, ok=False,
                detail=f"mcp — probe timed out after {timeout:g}s",
                hint="Check server URL/command and credentials",
            )
            for name in names
        ]
    except Exception as e:
        return [
            CheckResult(
                name, ok=False,
                detail=f"mcp — probe failed: {e}",
                hint="Check server URL/command and credentials",
            )
            for name in names
        ]


def _judge_mcp_server(name: str, srv: dict | None) -> CheckResult:
    """Turn one server's status snapshot into a CheckResult (unchanged semantics)."""
    if srv is None:
        return CheckResult(name, ok=False, detail="mcp — server not found")

    srv_status = srv.get("status", "unknown")
    if srv_status == "connected":
        tools = srv.get("tools", [])
        return CheckResult(name, ok=True, detail=f"mcp, {len(tools)} tools")
    elif srv_status == "needs-auth":
        return CheckResult(
            name, ok=False,
            detail="mcp — authentication required",
            hint="Check credentials in agent.yaml",
        )
    elif srv_status == "failed":
        error = srv.get("error", "unknown error")
        return CheckResult(
            name, ok=False,
            detail=f"mcp — {error}",
            hint="Check server URL/command and credentials",
        )
    else:
        return CheckResult(name, ok=False, detail=f"mcp — {srv_status}")


async def _async_probe_mcp(
    names: list[str],
    all_servers: dict[str, dict],
    project_path: Path,
) -> list[CheckResult]:
    from bobi.brain import get_brain
    from bobi.runtime_guard import prepare_brain_runtime

    # Build the probe session through the brain (Claude today). No system prompt
    # — this is a connect-and-poll MCP probe, not a turn. ``get_mcp_status`` is a
    # Claude-adapter capability (see #485 open Q4 on per-brain MCP discovery).
    prepare_brain_runtime(project_path)
    brain = get_brain()
    client = brain.make_session(
        cwd=str(project_path),
        system_prompt=None,
        options={
            "mcp_servers": all_servers,
            "strict_mcp_config": True,
            "max_turns": 0,
            # Probe in the same environment agents are spawned in so preflight can
            # never be green when the runtime PATH or runtime .env would differ.
            "env": child_agent_env(project_path),
        },
    )
    get_mcp_status = getattr(client, "get_mcp_status", None)
    if get_mcp_status is None:
        return [
            CheckResult(
                name, ok=False,
                detail=f"mcp — status probe not supported for {brain.name} brain",
                hint="MCP startup will be left to the agent runtime.",
                required=False,
            )
            for name in names
        ]
    try:
        timeout = preflight_timeout()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async def wait_remaining(awaitable_factory):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(awaitable_factory(), timeout=remaining)

        await wait_remaining(client.connect)
        status = await wait_remaining(get_mcp_status)

        # MDS-63: poll until every target server leaves the `pending` spawn
        # window (or the bounded budget elapses). Servers that genuinely
        # failed / need-auth report on the first non-pending poll, so real
        # failures add no latency.
        targets = set(names)
        for _ in range(_mcp_probe_max_polls()):
            servers = {s.get("name"): s for s in status.get("mcpServers", [])}
            still_pending = [
                n for n in targets
                if servers.get(n) is None or servers[n].get("status") == "pending"
            ]
            if not still_pending:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(MCP_PROBE_POLL_INTERVAL, remaining))
            try:
                status = await wait_remaining(get_mcp_status)
            except asyncio.TimeoutError:
                break

        servers = {s.get("name"): s for s in status.get("mcpServers", [])}
        return [_judge_mcp_server(name, servers.get(name)) for name in names]
    finally:
        try:
            disconnect_timeout = min(1.0, max(0.05, preflight_timeout()))
            await asyncio.wait_for(client.disconnect(), timeout=disconnect_timeout)
        except Exception:
            pass
