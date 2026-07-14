"""Long-term memory (#456) and the legacy per-agent decision log.

The team's durable, curated knowledge lives in a single, team-scoped
``<run>/state/long_term_memory.md`` — two sections (``## Facts`` / ``## Decisions``)
maintained out-of-band by the sleep-cycle monitor and injected read-only into
every agent's prompt. ``load_long_term_memory`` /
``format_long_term_memory_prompt`` are that path.

The older per-session decision log (``memory/<session>/INDEX.md``, an append-only
journal that bloated prompts and died with the agent) is being replaced by the
above. ``memory_dir_for_session`` is retained so the one-time seed can distill the
existing journals into the first long_term_memory.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Target size for the sleep cycle's normal compaction work. The hard cap below
# is a safety wall; the working budget keeps enough headroom that prompt
# injection never silently truncates hot memory state.
WORKING_MEMORY_CHARS = 16_000

# Hard cap on the injected long-term memory doc so it stays small and bounded — the whole
# point of #456 (the decision log it replaces grew to 127KB live and bloated
# every prompt). The sleep cycle keeps long_term_memory.md under this;
# load_long_term_memory truncates defensively as a backstop.
MAX_MEMORY_CHARS = 24_000

# Cap on legacy journal seed input. Raised from 8KB to 32KB for context rotation:
# the decision log is the primary continuity spine when sessions rotate,
# so it needs room for accumulated operational state.
MAX_LEGACY_MEMORY_CHARS = 32_000


def load_raw_long_term_memory(state_dir: Path) -> str:
    """Load long_term_memory.md without applying the prompt-injection cap.

    Scheduler-only callers use this so an over-cap artifact remains visible to
    the sleep cycle and can be compacted. It preserves the legacy policy.md
    migration behavior of load_long_term_memory().
    """
    from bobi import paths

    root = state_dir.parent if state_dir.name == "state" else None
    if root is not None:
        paths.migrate_long_term_memory_state(root)
    memory_file = state_dir / "long_term_memory.md"
    try:
        if not memory_file.is_file():
            return ""
        return memory_file.read_text()
    except OSError:
        log.debug("Failed to read long_term_memory.md at %s", memory_file, exc_info=True)
        return ""


def _truncate_to_budget(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    marker = "\n\n[memory truncated]"
    if budget <= len(marker):
        return marker[-budget:]
    return text[:budget - len(marker)] + marker


def _split_memory_sections(content: str) -> tuple[str, str] | None:
    facts_heading = "## Facts"
    decisions_heading = "## Decisions"
    facts_at: int | None = None
    decisions_at: int | None = None
    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == facts_heading and facts_at is None:
            facts_at = offset
        elif stripped == decisions_heading and facts_at is not None:
            decisions_at = offset
            break
        offset += len(line)
    if facts_at is None or decisions_at is None or decisions_at < facts_at:
        return None
    facts = content[facts_at:decisions_at].strip()
    decisions = content[decisions_at:].strip()
    if not facts or not decisions:
        return None
    return facts, decisions


def _section_aware_truncate(content: str, cap: int) -> str:
    sections = _split_memory_sections(content)
    if sections is None:
        return _truncate_to_budget(content, cap)

    facts, decisions = sections
    separator = "\n\n"
    available = cap - len(separator)
    if available <= 0:
        return _truncate_to_budget(content, cap)

    facts_budget = min(len(facts), available // 2)
    decisions_budget = min(len(decisions), available - facts_budget)
    remaining = available - facts_budget - decisions_budget
    if remaining and len(facts) > facts_budget:
        add = min(remaining, len(facts) - facts_budget)
        facts_budget += add
        remaining -= add
    if remaining and len(decisions) > decisions_budget:
        add = min(remaining, len(decisions) - decisions_budget)
        decisions_budget += add

    result = (
        f"{_truncate_to_budget(facts, facts_budget)}"
        f"{separator}"
        f"{_truncate_to_budget(decisions, decisions_budget)}"
    )
    return result[:cap]


def load_long_term_memory(state_dir: Path) -> str:
    """Load the team long_term_memory.md as one capped block, or "" when absent (#456).

    Reads ``state_dir/long_term_memory.md`` (the two-section ``## Facts`` / ``## Decisions``
    file the sleep cycle maintains), truncates at ``MAX_MEMORY_CHARS`` as a backstop,
    and returns "" if the file is missing or empty so callers can skip injection.
    Read-only — working agents never write this file.
    """
    content = load_raw_long_term_memory(state_dir).strip()
    if not content:
        return ""
    if len(content) > MAX_MEMORY_CHARS:
        log.warning("long_term_memory.md exceeds cap: %d chars (cap %d); "
                    "truncating prompt injection", len(content), MAX_MEMORY_CHARS)
        content = _section_aware_truncate(content, MAX_MEMORY_CHARS)
    return content


def format_long_term_memory_prompt(content: str) -> str:
    """Wrap long-term memory in a read-only prompt section for injection (#456).

    Returns "" for empty content so callers can skip injection. The section is
    marked read-only: it is maintained out-of-band by the sleep cycle, not edited
    by the working agent.
    """
    if not content:
        return ""
    return (
        "## Long-Term Memory\n\n"
        "Below is the team's curated, durable long-term memory (facts and decisions), "
        "maintained out-of-band by the sleep-cycle. It is **read-only** — do "
        "not edit it directly; the sleep cycle rewrites it from the team's "
        "transcripts. Use it for continuity and to avoid re-litigating settled "
        "decisions.\n\n"
        f"{content}"
    )


def memory_dir_for_session(state_dir: Path, session_name: str) -> Path:
    """Return the memory directory path for a given session."""
    return state_dir / "memory" / session_name


def load_memory(state_dir: Path, session_name: str) -> str:
    """Load a legacy decision-log index + notes for a session, as raw text.

    No longer injected into prompts (#456 replaced that with long-term memory).
    Retained for the one-time memory seed, which distills the existing
    memory/<session>/INDEX.md journal(s) into the first long_term_memory.md. Returns ""
    when no journal exists; truncates at MAX_LEGACY_MEMORY_CHARS as a safety bound.
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
    if len(combined) > MAX_LEGACY_MEMORY_CHARS:
        combined = combined[:MAX_LEGACY_MEMORY_CHARS] + "\n\n[memory truncated]"
    return combined


def collect_legacy_journals(state_dir: Path, budget: int) -> str:
    """Gather all per-session decision-log journals for the one-time seed (#456).

    Walks ``state_dir/memory/<session>/`` and concatenates each session's
    ``load_memory`` text, capped at ``budget`` chars total. Returns "" when no
    journals exist (a fresh team with nothing to seed). This feeds the sleep cycle's
    first run so the existing ~127KB of accumulated knowledge is distilled into
    the first ``long_term_memory.md`` rather than discarded; after ``long_term_memory.md`` exists the
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


# Deprecated aliases kept for one release while installed packages catch up.
MAX_POLICY_CHARS = MAX_MEMORY_CHARS


def load_policy(state_dir: Path) -> str:
    return load_long_term_memory(state_dir)


def format_policy_prompt(content: str) -> str:
    return format_long_term_memory_prompt(content)
