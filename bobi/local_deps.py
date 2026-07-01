"""Local dependency materialization (#428 Stage 5).

`bobi agents install --with-deps` closes the **local-dev** story. Today install
only *composes* a team's package; it never runs `install:`/`guide:`, so a
locally-run team hard-blocks at the `requires:`/MCP dispatch gate
(`subagent.check_requires`) until the developer hand-installs every tool. This
module lets the brain **already present on the developer's machine** do that
install, driven by the same `guide`/`install`/`success` contract CI uses (the
`dep_bootstrap` cold path), but pointed at the **host root** instead of a
throwaway container.

Why an agent and not a ported recipe: `install:` recipes are container-shaped
(Debian apt / `/opt` / root). Replaying them verbatim on an arbitrary host
(macOS, non-sudo) is exactly the brittle per-host maintenance the framework
rejects. The agent adapts them to the real host (brew / apt / pipx / a binary
into `~/.local/bin`), exactly as a human would.

The central concern is **host mutation, not sudo**: this writes to the dev's
real machine, so it is confirm-gated, previews its plan, and records a
transcript of what the agent ran. Almost every dependency is userland and needs
no sudo; the slim set that does is surfaced as an explicit confirm, never
silently escalated, and `host:` capabilities (a kernel sysctl) stay the
guided-fix path (like `doctor --fix`). It is idempotent — a dependency whose
`success` already passes is skipped — and partial failure is non-fatal, because
doctor and the dispatch preflight still gate.

The orchestration is pure over injected runners (`agent_runner`/`shell_runner`)
so it is unit-testable without a real brain; the CLI wires the real ones and
owns the interactive confirm/preview.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field

from bobi.dep_bootstrap import (
    AgentRunner,
    ShellRunner,
    _parse_recipe_verdict,
    default_agent_runner,
    default_shell_runner,
    preflight,
)
from bobi.tool_library import Dependency

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class DepPlan:
    """What a single dependency needs on this host, decided before any mutation."""

    dep: Dependency
    satisfied: bool          # its `success` already passes — skip it (idempotent)
    needs_sudo: bool         # its install shape implies a system (sudo) step
    detail: str = ""         # why it's (un)satisfied — the preflight detail

    @property
    def materializable(self) -> bool:
        """True if there is something to run for it (an install or a guide)."""
        return bool(self.dep.install or self.dep.guide.strip())


@dataclass
class LocalPlan:
    """The full pass over a team's dependency set: what's done, what's to do."""

    satisfied: list[DepPlan] = field(default_factory=list)
    todo: list[DepPlan] = field(default_factory=list)
    # Dependencies with nothing to materialize AND not satisfied — a declaration
    # error (no install, no guide) surfaced rather than silently ignored.
    unmaterializable: list[DepPlan] = field(default_factory=list)

    @property
    def needs_sudo(self) -> bool:
        """Any to-do dependency whose install shape implies a sudo step."""
        return any(p.needs_sudo for p in self.todo)

    @property
    def nothing_to_do(self) -> bool:
        return not self.todo and not self.unmaterializable


def _install_needs_sudo(dep: Dependency) -> bool:
    """Whether a dependency's pinned install implies a system (sudo) step.

    `apt` is a system package manager and `run_root` runs as root — both need
    sudo on a real host. A guide-only dependency defaults to userland (the agent
    is instructed not to escalate), so it is not flagged here.
    """
    install = dep.install or {}
    return bool(install.get("apt") or install.get("run_root"))


def verify(dep: Dependency, *, brain: str, shell_runner: ShellRunner,
           base_env: dict | None = None) -> tuple[bool, str]:
    """Run `dep.success` in the RUNTIME tier under `brain`; return (ok, detail).

    Reuses `dep_bootstrap.preflight` with ``phase="runtime"`` (not the build
    tier) so a contract that gates on the phase — the migrated `codex` entry —
    verifies against a real, credentialed check, the same one the dispatch
    preflight enforces.
    """
    results = preflight(dep, brains=[brain], shell_runner=shell_runner,
                        base_env=base_env, phase="runtime")
    r = results[0]
    return r.ok, r.detail


def plan_dependencies(deps: list[Dependency], *, brain: str,
                      shell_runner: ShellRunner | None = None,
                      base_env: dict | None = None) -> LocalPlan:
    """Classify each dependency against this host before any mutation.

    Runs every dependency's `success` (runtime tier) so an already-satisfied one
    is skipped (idempotency), and partitions the rest into materializable to-do
    and an unmaterializable bucket (declared with neither install nor guide).
    """
    shell_runner = shell_runner or default_shell_runner
    plan = LocalPlan()
    for dep in deps:
        ok, detail = verify(dep, brain=brain, shell_runner=shell_runner,
                            base_env=base_env)
        dp = DepPlan(dep=dep, satisfied=ok,
                     needs_sudo=_install_needs_sudo(dep), detail=detail)
        if ok:
            plan.satisfied.append(dp)
        elif dp.materializable:
            plan.todo.append(dp)
        else:
            plan.unmaterializable.append(dp)
    return plan


# ---------------------------------------------------------------------------
# Materialize (host-adapting agent)
# ---------------------------------------------------------------------------


def _host_os() -> str:
    """A friendly host-OS label for the install prompt."""
    sys = platform.system()
    return {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}.get(sys, sys)


def build_install_prompt(dep: Dependency, *, host_os: str,
                         allow_sudo: bool) -> str:
    """Instruction for an agent installing `dep` on the developer's real host.

    Unlike the container bootstrap prompt (`dep_bootstrap.build_bootstrap_prompt`,
    which materializes into a throwaway image and reports a frozen recipe), this
    agent mutates a real machine and ADAPTS any pinned `install` to the host —
    the recipe is a version-pin reference, not verbatim commands. It reports the
    exact commands it ran as a transcript, so the mutation is auditable.
    """
    import json as _json

    parts = [
        f"You are installing a software dependency on a developer's LOCAL "
        f"machine (host OS: {host_os}). The agent brain CLI is already "
        f"installed. Install and configure ONLY this dependency so its success "
        f"condition holds on THIS host, then verify it yourself before "
        f"reporting.",
        f"Dependency name: {dep.name}",
        f"Success condition (must hold when you are done):\n{dep.success}",
    ]
    if dep.install:
        parts.append(
            "Reference install steps (container-shaped: these are the "
            "authoritative VERSION PINS, not verbatim commands — ADAPT the "
            "commands to this host):\n"
            + _json.dumps(dep.install, indent=2))
    if dep.guide.strip():
        parts.append(f"Guide (how to materialize and use it):\n{dep.guide}")

    sudo_rule = (
        "- You MAY use sudo for a system package manager if there is no userland "
        "alternative, but prefer userland."
        if allow_sudo else
        "- Do NOT use sudo. If a step truly requires root, STOP and report "
        '"ok": false with exactly what needs root in `notes`; do not escalate.'
    )
    parts.append(
        "Rules:\n"
        "- Adapt to THIS host's conventions and PREFER userland installs "
        "(npm `-g` into a user prefix, pipx, brew, a binary into "
        "`~/.local/bin`). On macOS use brew, never apt.\n"
        "- PIN versions wherever the reference steps or guide pin them; never "
        "substitute a floating/`latest` reference for a pinned one.\n"
        f"{sudo_rule}\n"
        "- Do NOT modify kernel/system capabilities (sysctl, devices) or touch "
        "credentials; those are handled separately.\n"
        "- Do the minimum to make the success condition true, then verify it "
        "yourself.")
    parts.append(
        "When finished, output as the VERY LAST line a single line of JSON, "
        "with nothing after it, in exactly this form:\n"
        '{"ok": true, "ran": ["<each shell command you actually ran>"], '
        '"notes": "<what you did / any caveat>"}\n'
        'Use "ok": false if you could not make the success condition true.')
    return "\n\n".join(parts)


@dataclass
class DepResult:
    """Outcome of materializing one dependency on the host."""

    dep: str
    ok: bool
    skipped: bool = False        # already satisfied — no agent ran
    detail: str = ""
    transcript: list[str] = field(default_factory=list)
    notes: str = ""


def materialize_dependency(dp: DepPlan, *, brain: str, allow_sudo: bool,
                           agent_runner: AgentRunner,
                           shell_runner: ShellRunner,
                           base_env: dict | None = None) -> DepResult:
    """Drive the local brain to install one dependency, then re-verify `success`.

    The agent's own ``ok`` claim is advisory; the source of truth is a fresh
    runtime-tier `success` check after it finishes (the same contract the
    dispatch preflight enforces), so an agent that declares victory on a
    half-install is caught.
    """
    dep = dp.dep
    prompt = build_install_prompt(dep, host_os=_host_os(), allow_sudo=allow_sudo)
    try:
        text = agent_runner(prompt, brain)
    except Exception as exc:  # a crashed install agent is a failed materialize
        log.warning("Install agent for %s crashed: %s", dep.name, exc)
        return DepResult(dep=dep.name, ok=False,
                         detail=f"install agent crashed: {exc}")

    verdict = _parse_recipe_verdict(text) or {}
    ran = [str(c) for c in (verdict.get("ran") or []) if str(c).strip()]
    notes = str(verdict.get("notes", "") or "")

    ok, detail = verify(dep, brain=brain, shell_runner=shell_runner,
                        base_env=base_env)
    if not ok and not verdict:
        detail = detail or "install agent produced no parseable result"
    return DepResult(dep=dep.name, ok=ok, detail=detail, transcript=ran,
                     notes=notes)


def install_dependencies(todo: list[DepPlan], *, brain: str, allow_sudo: bool,
                         agent_runner: AgentRunner | None = None,
                         shell_runner: ShellRunner | None = None,
                         base_env: dict | None = None) -> list[DepResult]:
    """Materialize every to-do dependency in order; never raises on a failure.

    A dependency that fails is reported and the pass continues — partial failure
    is non-fatal (doctor / the dispatch preflight still gate), matching the
    idempotent, re-runnable design.
    """
    agent_runner = agent_runner or default_agent_runner
    shell_runner = shell_runner or default_shell_runner
    results: list[DepResult] = []
    for dp in todo:
        results.append(materialize_dependency(
            dp, brain=brain, allow_sudo=allow_sudo, agent_runner=agent_runner,
            shell_runner=shell_runner, base_env=base_env))
    return results
