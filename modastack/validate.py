"""Startup config validation.

Runs before the agent session starts to catch misconfigurations early:
role exists, credentials present, MCP servers connect, Venn services
are logged in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    ok: bool
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def errors(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    def format(self) -> str:
        lines = []
        for c in self.checks:
            icon = "✓" if c.ok else "✗"
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


def validate_config(
    project_path: Path,
    agent_name: str | None = None,
) -> ValidationResult:
    """Run all startup validation checks."""
    from modastack.config import Config

    cfg = Config.load(project_path, agent_name=agent_name)
    checks: list[CheckResult] = []

    checks.append(_check_entry_point(cfg, project_path, agent_name))
    checks.extend(_check_native_credentials(cfg))
    checks.extend(_check_venn_services(cfg))
    checks.extend(_check_mcp_servers(cfg, project_path))

    return ValidationResult(
        ok=all(c.ok for c in checks),
        checks=checks,
    )


def _check_entry_point(cfg, project_path: Path, agent_name: str | None) -> CheckResult:
    if not cfg.entry_point:
        return CheckResult("entry_point", ok=True, detail="not set, defaulting to manager")

    installed_roles = project_path / ".modastack" / "roles"
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


def _check_native_credentials(cfg) -> list[CheckResult]:
    checks = []
    for svc in cfg.services:
        if svc.name == "slack":
            ok = bool(cfg.slack_bot_token)
            checks.append(CheckResult(
                "slack", ok=ok,
                detail="native" if ok else "native — missing bot token",
                hint="Set slack.bot_token in agent.yaml" if not ok else "",
            ))
        elif svc.name == "linear":
            ok = bool(cfg.linear_api_key)
            checks.append(CheckResult(
                "linear", ok=ok,
                detail="native" if ok else "native — missing API key",
                hint="Set linear.api_key in agent.yaml" if not ok else "",
            ))
        elif svc.name == "github":
            checks.append(CheckResult("github", ok=True, detail="native"))
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
            )
            for s in venn_services
        ]

    from modastack.venn import check_services
    result = check_services(cfg.venn_api_key, [s.name for s in venn_services])

    checks = []
    for name in result.connected:
        checks.append(CheckResult(name, ok=True, detail="venn"))
    for name in result.missing:
        checks.append(CheckResult(
            name, ok=False,
            detail="venn — not connected",
            hint="Connect at venn.ai, then restart",
        ))
    return checks


def _check_mcp_servers(cfg, project_path: Path) -> list[CheckResult]:
    if not cfg.mcp_servers:
        return []

    checks = []
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
            if not server_cfg.get("command"):
                checks.append(CheckResult(
                    name, ok=False,
                    detail="mcp — missing command",
                    hint=f"Set mcp_servers.{name}.command in agent.yaml",
                ))
                continue

        check = _probe_mcp_server(name, cfg.mcp_servers, project_path)
        checks.append(check)

    return checks


def _probe_mcp_server(
    name: str,
    all_servers: dict[str, dict],
    project_path: Path,
) -> CheckResult:
    """Connect to an MCP server via the Claude SDK and verify it lists tools."""
    try:
        result = asyncio.run(_async_probe_mcp(name, all_servers, project_path))
        return result
    except Exception as e:
        return CheckResult(
            name, ok=False,
            detail=f"mcp — probe failed: {e}",
            hint="Check server URL/command and credentials",
        )


async def _async_probe_mcp(
    name: str,
    all_servers: dict[str, dict],
    project_path: Path,
) -> CheckResult:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from modastack.sdk import get_cli_path

    options = ClaudeAgentOptions(
        cwd=str(project_path),
        permission_mode="bypassPermissions",
        cli_path=get_cli_path(),
        mcp_servers=all_servers,
        strict_mcp_config=True,
        max_turns=0,
    )

    client = ClaudeSDKClient(options)
    try:
        await client.connect()
        status = await client.get_mcp_status()

        for server in status.get("mcpServers", []):
            if server.get("name") != name:
                continue

            srv_status = server.get("status", "unknown")
            if srv_status == "connected":
                tools = server.get("tools", [])
                return CheckResult(
                    name, ok=True,
                    detail=f"mcp, {len(tools)} tools",
                )
            elif srv_status == "needs-auth":
                return CheckResult(
                    name, ok=False,
                    detail="mcp — authentication required",
                    hint="Check credentials in agent.yaml",
                )
            elif srv_status == "failed":
                error = server.get("error", "unknown error")
                return CheckResult(
                    name, ok=False,
                    detail=f"mcp — {error}",
                    hint="Check server URL/command and credentials",
                )
            else:
                return CheckResult(
                    name, ok=False,
                    detail=f"mcp — {srv_status}",
                )

        return CheckResult(
            name, ok=False,
            detail="mcp — server not found",
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
