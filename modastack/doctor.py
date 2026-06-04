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
                       hint="Run `modastack init` to create it")


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
