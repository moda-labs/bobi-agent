"""Agent decision log (memory) — per-agent persistent notes store.

Each agent gets a memory directory at .modastack/state/memory/<session-name>/
containing an INDEX.md (YAML current-state block + prose notes) and optional
per-topic note files. The framework loads the index at every session start
so decisions survive --fresh and session rotation.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Cap on injected memory. Raised from 8KB to 32KB for context rotation:
# the decision log is the primary continuity spine when sessions rotate,
# so it needs room for accumulated operational state.
MAX_MEMORY_CHARS = 32_000


def memory_dir_for_session(state_dir: Path, session_name: str) -> Path:
    """Return the memory directory path for a given session."""
    return state_dir / "memory" / session_name


def load_memory(state_dir: Path, session_name: str) -> str:
    """Load the memory index + notes for a session, formatted as text.

    Returns empty string if no memory exists. Truncates if content exceeds
    MAX_MEMORY_CHARS to prevent prompt bloat.
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
    # Warn when the log is large relative to the cap — entering a rotation
    # loop is likely if memory grows much further.
    if len(combined) > MAX_MEMORY_CHARS // 2:
        log.warning(
            "Decision log for %s is %d chars (%.0f%% of %d cap) — "
            "consider pruning to avoid prompt bloat",
            session_name, len(combined),
            100 * len(combined) / MAX_MEMORY_CHARS, MAX_MEMORY_CHARS,
        )
    return combined


def format_memory_prompt(content: str) -> str:
    """Wrap memory content in a prompt section for injection.

    Returns empty string if no content, so callers can skip injection.
    """
    if not content:
        return ""
    return (
        "## Decision Log\n\n"
        "Below is your persistent decision log from previous sessions. "
        "It contains decisions you've made and context you've recorded. "
        "Use it to maintain continuity.\n\n"
        f"{content}"
    )
