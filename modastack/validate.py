"""Startup config validation.

Runs before the agent session starts to catch misconfigurations early:
role exists, credentials present, MCP servers connect, Venn services
are logged in.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from modastack.env import agent_spawn_env

log = logging.getLogger(__name__)

# Stdio MCP servers spawn a subprocess and run their MCP `initialize` handshake
# (~1–2s), so a single status read catches them mid-spawn in the `pending`
# window and false-fails them (MDS-63). Poll until each target server leaves
# `pending`, bounded by a hard ~10s ceiling (0.5s × 20).
MCP_PROBE_POLL_INTERVAL = 0.5
MCP_PROBE_MAX_POLLS = 20


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
    from modastack.config import Config

    cfg = Config.load(project_path)
    checks: list[CheckResult] = []

    checks.append(_check_entry_point(cfg, project_path))
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

    from modastack import paths
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


def _check_service_credentials(cfg) -> list[CheckResult]:
    """Validate declared credentials for all services with registered adapters.

    Every declared credential key must interpolate to a non-empty value.
    Services with no declared credentials (e.g. github — auth rides on `gh`)
    pass automatically.
    """
    from modastack.events.adapters import is_registered

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

    from modastack.venn import check_services
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
    from modastack.brain import get_brain

    # Build the probe session through the brain (Claude today). No system prompt
    # — this is a connect-and-poll MCP probe, not a turn. ``get_mcp_status`` is a
    # Claude-adapter capability (see #485 open Q4 on per-brain MCP discovery).
    brain = get_brain()
    client = brain.make_session(
        cwd=str(project_path),
        system_prompt=None,
        options={
            "mcp_servers": all_servers,
            "strict_mcp_config": True,
            "max_turns": 0,
            # Probe in the same environment agents are spawned in so preflight can
            # never be green when the runtime PATH would fail to resolve a bare
            # command (MDS-64).
            "env": agent_spawn_env(),
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
        await client.connect()
        status = await get_mcp_status()

        # MDS-63: poll until every target server leaves the `pending` spawn
        # window (or the bounded budget elapses). Servers that genuinely
        # failed / need-auth report on the first non-pending poll, so real
        # failures add no latency.
        targets = set(names)
        for _ in range(MCP_PROBE_MAX_POLLS):
            servers = {s.get("name"): s for s in status.get("mcpServers", [])}
            still_pending = [
                n for n in targets
                if servers.get(n) is None or servers[n].get("status") == "pending"
            ]
            if not still_pending:
                break
            await asyncio.sleep(MCP_PROBE_POLL_INTERVAL)
            status = await get_mcp_status()

        servers = {s.get("name"): s for s in status.get("mcpServers", [])}
        return [_judge_mcp_server(name, servers.get(name)) for name in names]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
