"""Render the team's global instructions (package ``AGENTS.md``) into the
locations each brain natively auto-loads (#779).

Repo-level instruction files open with "Read ``~/AGENTS.md`` first" - a pointer
that dangles inside a deployed team's container, where nothing materializes the
operator's unversioned ``~/AGENTS.md``. The fix is first-class: the team package
ships a root-level ``AGENTS.md`` (sibling of ``agent.yaml``, installed to
``run/package/AGENTS.md``), and at process bootstrap (manager boot, subagent
child entry) the runtime renders it to every path the active brain auto-loads
as global guidance:

  * ``~/AGENTS.md`` - always; satisfies the explicit pointer in repo files.
  * ``$CODEX_HOME/AGENTS.md`` - codex brains; Codex loads it in every repo.
  * ``$CLAUDE_CONFIG_DIR/CLAUDE.md`` - claude/gateway brains; Claude Code loads
    user memory in every repo.

Same shape as the #428 Stage 4 per-brain MCP rendering (``codex_config.py``):
the package declares it once, the runtime renders it brain-natively. The
rendered text lives inside a **managed block** so foreign content survives
verbatim - this matters most for ``$CLAUDE_CONFIG_DIR/CLAUDE.md``, where the
agent's own ``#``-memory writes land in the same file. Writes are atomic and
idempotent (no disk churn when unchanged), and a team that ships no
``AGENTS.md`` removes a previously managed block (same lifecycle as the MCP
block). A brain-kind switch also cleans the previous brain's target, so a
retired block never keeps feeding a brain the team no longer runs.

The render runs at runtime, after the deploy entrypoint has re-linked the brain
config dirs onto the durable volume - nothing baked at image-build time would
survive those symlinks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bobi import paths

log = logging.getLogger(__name__)

# Sentinels bracketing the bobi-owned region of each target file. HTML comments
# so they are inert in markdown. Everything outside them is foreign content
# preserved untouched; everything between them is regenerated on each render.
MANAGED_BEGIN = "<!-- >>> bobi-managed team instructions (do not edit) >>> -->"
MANAGED_END = "<!-- <<< bobi-managed team instructions <<< -->"

# The well-known package input, at the package root (sibling of agent.yaml).
PACKAGE_AGENTS_MD = "AGENTS.md"


def claude_config_dir() -> Path:
    """The directory Claude Code loads user memory (``CLAUDE.md``) from
    (``$CLAUDE_CONFIG_DIR`` or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def instruction_targets(brain_kind: str) -> list[Path]:
    """The files brain *brain_kind* auto-loads as global instructions.

    ``~/AGENTS.md`` applies to every brain (it is what repo instruction files
    point at); the brain-native path is added per kind. The gateway brain runs
    the Claude CLI, so it reads Claude's user memory. A new brain kind that
    auto-loads a global-instructions file must be added here, or its teams'
    house rules silently never load - the bug this module exists to fix.
    """
    targets = [Path.home() / "AGENTS.md"]
    if brain_kind == "codex":
        from bobi.brain.codex_config import codex_home

        targets.append(codex_home() / "AGENTS.md")
    elif brain_kind in ("claude", "gateway"):
        targets.append(claude_config_dir() / "CLAUDE.md")
    return targets


def _all_brain_targets() -> set[Path]:
    """Every target any known brain kind auto-loads, for cross-kind cleanup."""
    from bobi.brain import known_brain_kinds

    out: set[Path] = set()
    for kind in known_brain_kinds():
        out.update(instruction_targets(kind))
    return out


def _is_marker(line: str) -> bool:
    return line.strip() in (MANAGED_BEGIN, MANAGED_END)


def _strip_managed(text: str) -> str:
    """Remove every bobi-managed block (markers inclusive), preserving all
    other content.

    A ``MANAGED_BEGIN`` with no closing marker (a truncated write, a hand-edit)
    drops everything to end-of-file rather than letting stale managed content
    leak into the foreign region and duplicate on the next render.
    """
    out: list[str] = []
    in_managed = False
    for line in text.splitlines():
        s = line.strip()
        if s == MANAGED_BEGIN:
            in_managed = True
            continue
        if s == MANAGED_END:
            in_managed = False
            continue
        if in_managed:
            continue
        out.append(line)
    return "\n".join(out)


def render_instructions(existing: str, content: str) -> str:
    """Render *content* as the managed block of *existing* file text.

    The previous managed block (if any) is removed and the current content
    re-rendered inside a managed block appended at the end; foreign content is
    preserved. Empty *content* removes the block entirely. Marker lines inside
    *content* itself are dropped - an embedded sentinel (a team documenting
    this very mechanism) would otherwise terminate the block early and leak
    stale managed text into the foreign region on the next re-render.
    """
    foreign = _strip_managed(existing).rstrip("\n")
    body = "\n".join(
        l for l in content.strip("\n").splitlines() if not _is_marker(l)
    ).strip("\n")
    if not body:
        return foreign + "\n" if foreign else ""
    block = f"{MANAGED_BEGIN}\n{body}\n{MANAGED_END}"
    return (foreign + "\n\n" if foreign else "") + block + "\n"


def has_managed_block(path: Path) -> bool:
    """True if *path* carries a bobi-managed block - the signal that a stale
    render needs cleaning even when the team now ships no ``AGENTS.md``.

    Matched line-wise with the same rule ``_strip_managed`` uses, so a file
    that merely mentions the sentinel text inline is not treated as managed
    (and re-written on every boot) when the strip would not actually touch it.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return any(line.strip() == MANAGED_BEGIN for line in text.splitlines())


def write_instructions(path: Path, content: str) -> Path:
    """Render *content* into *path*'s managed block, preserving foreign text.

    Idempotent: re-rendering the same content reproduces the same file, and
    only touches disk when the rendered text actually changes (avoids churning
    the durable volume + mtime on every boot). The write is atomic (temp file
    + rename): these targets carry foreign content bobi must never lose - a
    process killed mid-write would otherwise truncate e.g. Claude's
    accumulated ``#``-memory in CLAUDE.md. Returns *path*.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    rendered = render_instructions(existing, content)
    if rendered != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.bobi-tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, path)
    return path


def render_team_instructions(
    project_path: Path, brain_kind: str | None = None,
) -> list[Path]:
    """Render the installed package's ``AGENTS.md`` to every applicable target.

    The process-bootstrap hook (manager boot in ``run_manager_from_config``,
    subagent child entry in ``_run_agent_entry``): reads the frozen
    ``run/package/AGENTS.md`` and writes each target the active brain
    auto-loads; targets only OTHER brain kinds auto-load get a previously
    managed block removed (a brain-kind switch must not leave the old brain
    reading retired rules). A target is touched only when there is content to
    render OR a managed block to clean - a team that never shipped
    instructions never creates or edits the files.

    Errors propagate - a team that ships house rules and can't render them
    would otherwise silently build without the standards its PRs are supposed
    to meet - EXCEPT for a target whose existing (foreign-owned) bytes don't
    decode: one stray non-UTF-8 byte in an operator's dotfile must not
    crash-loop every boot, so that target is skipped with a warning.

    Returns the list of targets written or cleaned this call.
    """
    if brain_kind is None:
        from bobi.brain import get_brain

        brain_kind = get_brain().name
    src = paths.package_dir(project_path) / PACKAGE_AGENTS_MD
    content = src.read_text(encoding="utf-8") if src.is_file() else ""
    active = instruction_targets(brain_kind)
    written: list[Path] = []
    for target in active:
        if _apply(target, content):
            written.append(target)
    for target in sorted(_all_brain_targets() - set(active)):
        if _apply(target, ""):
            written.append(target)
    if written:
        log.info(
            "Rendered team instructions (%s) to: %s",
            "package AGENTS.md" if content else "removal",
            ", ".join(str(p) for p in written),
        )
    return written


def _apply(target: Path, content: str) -> bool:
    """Render *content* into *target* if there is anything to do.

    Returns whether the target was written/cleaned. Undecodable existing
    bytes (foreign-owned file) skip the target with a warning instead of
    propagating - see :func:`render_team_instructions`.
    """
    try:
        if not (content or has_managed_block(target)):
            return False
        write_instructions(target, content)
        return True
    except UnicodeDecodeError as e:
        log.warning(
            "Skipping team-instructions render for %s: existing content is "
            "not valid UTF-8 (%s)", target, e,
        )
        return False
