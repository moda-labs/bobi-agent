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

# Hard cap on the injected long-term memory doc so it stays small and bounded — the whole
# point of #456 (the decision log it replaces grew to 127KB live and bloated
# every prompt). The sleep cycle keeps long_term_memory.md under this;
# load_long_term_memory truncates defensively as a backstop.
MAX_MEMORY_CHARS = 24_000

# Cap on legacy journal seed input. Raised from 8KB to 32KB for context rotation:
# the decision log is the primary continuity spine when sessions rotate,
# so it needs room for accumulated operational state.
MAX_LEGACY_MEMORY_CHARS = 32_000
_TRUNCATION_MARKER = "[memory truncated]"


def load_long_term_memory_uncapped(state_dir: Path) -> str:
    """Load long_term_memory.md without applying the prompt-injection cap.

    Intended for the sleep-cycle scheduler, which must show the curator the full
    artifact when it needs compaction. Working-agent prompt injection should use
    ``load_long_term_memory`` instead.
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
    except (OSError, UnicodeDecodeError):
        log.debug("Failed to read long_term_memory.md at %s", memory_file, exc_info=True)
        return ""


def _split_long_term_memory_sections(content: str) -> tuple[str, str] | None:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.splitlines(keepends=True):
        heading = line.strip()
        if heading == "## Facts":
            current = "facts"
            sections[current] = []
            continue
        if heading == "## Decisions":
            current = "decisions"
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)

    if "facts" not in sections or "decisions" not in sections:
        return None
    return ("".join(sections["facts"]).strip(), "".join(sections["decisions"]).strip())


def _allocate_section_budgets(facts: str, decisions: str, total: int) -> tuple[int, int]:
    facts_budget = total // 2
    decisions_budget = total - facts_budget

    if len(facts) < facts_budget:
        decisions_budget += facts_budget - len(facts)
        facts_budget = len(facts)
    if len(decisions) < decisions_budget:
        facts_budget += decisions_budget - len(decisions)
        decisions_budget = len(decisions)

    return max(facts_budget, 0), max(decisions_budget, 0)


def _truncate_section(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    marker = "\n\n" + _TRUNCATION_MARKER
    keep = max(budget - len(marker), 0)
    return text[:keep].rstrip() + marker


def _truncate_long_term_memory_for_prompt(content: str) -> str:
    sections = _split_long_term_memory_sections(content)
    if sections is None:
        return _truncate_section(content, MAX_MEMORY_CHARS)

    facts, decisions = sections
    facts_header = "## Facts\n\n"
    decisions_header = "\n\n## Decisions\n\n"
    fixed = len(facts_header) + len(decisions_header)
    available = max(MAX_MEMORY_CHARS - fixed, 0)
    facts_budget, decisions_budget = _allocate_section_budgets(facts, decisions, available)
    return (
        facts_header
        + _truncate_section(facts, facts_budget)
        + decisions_header
        + _truncate_section(decisions, decisions_budget)
    )


def load_long_term_memory(state_dir: Path) -> str:
    """Load the team long_term_memory.md as one capped block, or "" when absent (#456).

    Reads ``state_dir/long_term_memory.md`` (the two-section ``## Facts`` / ``## Decisions``
    file the sleep cycle maintains), truncates at ``MAX_MEMORY_CHARS`` as a backstop,
    and returns "" if the file is missing or empty so callers can skip injection.
    Read-only — working agents never write this file.
    """
    from bobi import paths

    root = state_dir.parent if state_dir.name == "state" else None
    if root is not None:
        paths.migrate_long_term_memory_state(root)
    memory_file = state_dir / "long_term_memory.md"
    try:
        if not memory_file.is_file():
            return ""
        content = memory_file.read_text().strip()
    except (OSError, UnicodeDecodeError):
        log.debug("Failed to read long_term_memory.md at %s", memory_file, exc_info=True)
        return ""
    if not content:
        return ""
    if len(content) > MAX_MEMORY_CHARS:
        log.warning(
            "long_term_memory.md at %s is %d chars (over %d cap); truncating for prompt",
            memory_file, len(content), MAX_MEMORY_CHARS,
        )
        content = _truncate_long_term_memory_for_prompt(content)
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
