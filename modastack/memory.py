"""Team policy (#456) and the legacy per-agent decision log.

The team's durable, curated knowledge lives in a single, team-scoped
``.modastack/state/policy.md`` — two sections (``## Facts`` / ``## Decisions``)
maintained out-of-band by the policy-curator monitor and injected read-only into
every agent's prompt. ``load_policy`` / ``format_policy_prompt`` are that path.

The older per-session decision log (``memory/<session>/INDEX.md``, an append-only
journal that bloated prompts and died with the agent) is being replaced by the
above. ``memory_dir_for_session`` is retained so the one-time seed can distill the
existing journals into the first policy.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Hard cap on the injected policy doc so it stays small and bounded — the whole
# point of #456 (the decision log it replaces grew to 127KB live and bloated
# every prompt). The curator keeps policy.md under this; load_policy truncates
# defensively as a backstop.
MAX_POLICY_CHARS = 24_000

# Cap on injected memory. Raised from 8KB to 32KB for context rotation:
# the decision log is the primary continuity spine when sessions rotate,
# so it needs room for accumulated operational state.
MAX_MEMORY_CHARS = 32_000


def load_policy(state_dir: Path) -> str:
    """Load the team policy.md as one capped block, or "" when absent (#456).

    Reads ``state_dir/policy.md`` (the two-section ``## Facts`` / ``## Decisions``
    file the curator maintains), truncates at ``MAX_POLICY_CHARS`` as a backstop,
    and returns "" if the file is missing or empty so callers can skip injection.
    Read-only — working agents never write this file.
    """
    policy_file = state_dir / "policy.md"
    try:
        if not policy_file.is_file():
            return ""
        content = policy_file.read_text().strip()
    except OSError:
        log.debug("Failed to read policy.md at %s", policy_file, exc_info=True)
        return ""
    if not content:
        return ""
    if len(content) > MAX_POLICY_CHARS:
        content = content[:MAX_POLICY_CHARS] + "\n\n[policy truncated]"
    return content


def format_policy_prompt(content: str) -> str:
    """Wrap policy content in a read-only prompt section for injection (#456).

    Returns "" for empty content so callers can skip injection. The section is
    marked read-only: it is maintained out-of-band by the curator, not edited
    by the working agent.
    """
    if not content:
        return ""
    return (
        "## Team Policy\n\n"
        "Below is the team's curated, durable policy (facts and decisions), "
        "maintained out-of-band by the policy-curator. It is **read-only** — do "
        "not edit it directly; the curator rewrites it from the team's "
        "transcripts. Use it for continuity and to avoid re-litigating settled "
        "decisions.\n\n"
        f"{content}"
    )


def memory_dir_for_session(state_dir: Path, session_name: str) -> Path:
    """Return the memory directory path for a given session."""
    return state_dir / "memory" / session_name


def load_memory(state_dir: Path, session_name: str) -> str:
    """Load a legacy decision-log index + notes for a session, as raw text.

    No longer injected into prompts (#456 replaced that with the team policy).
    Retained for the one-time policy seed, which distills the existing
    memory/<session>/INDEX.md journal(s) into the first policy.md. Returns ""
    when no journal exists; truncates at MAX_MEMORY_CHARS as a safety bound.
    """
    mem_dir = memory_dir_for_session(state_dir, session_name)
    if not mem_dir.is_dir():
        return ""

    parts: list[str] = []

    # Load the index file (primary content)
    index_path = mem_dir / "INDEX.md"
    if index_path.is_file():
        content = index_path.read_text().strip()
        if content:
            parts.append(content)

    # Load individual note files (sorted for determinism)
    for note in sorted(mem_dir.glob("*.md")):
        if note.name == "INDEX.md":
            continue
        try:
            text = note.read_text().strip()
            if text:
                parts.append(f"### {note.stem}\n\n{text}")
        except OSError:
            log.debug("Failed to read note %s", note)

    if not parts:
        return ""

    combined = "\n\n".join(parts)
    if len(combined) > MAX_MEMORY_CHARS:
        combined = combined[:MAX_MEMORY_CHARS] + "\n\n[memory truncated]"
    return combined


def collect_legacy_journals(state_dir: Path, budget: int) -> str:
    """Gather all per-session decision-log journals for the one-time seed (#456).

    Walks ``state_dir/memory/<session>/`` and concatenates each session's
    ``load_memory`` text, capped at ``budget`` chars total. Returns "" when no
    journals exist (a fresh team with nothing to seed). This feeds the curator's
    first run so the existing ~127KB of accumulated knowledge is distilled into
    the first ``policy.md`` rather than discarded; after ``policy.md`` exists the
    seed never runs again (the caller guards on absence).
    """
    mem_root = state_dir / "memory"
    if not mem_root.is_dir():
        return ""

    parts: list[str] = []
    used = 0
    for session_dir in sorted(p for p in mem_root.iterdir() if p.is_dir()):
        text = load_memory(state_dir, session_dir.name).strip()
        if not text:
            continue
        block = f"### legacy journal: {session_dir.name}\n\n{text}"
        if used + len(block) > budget:
            # Stop at the budget — the seed is a one-shot bounded by
            # MAX_SEED_INPUT_CHARS, far above the journal's real size.
            parts.append("\n[seed input truncated at budget — remaining journals omitted]")
            break
        parts.append(block)
        used += len(block)

    return "\n\n".join(parts)
