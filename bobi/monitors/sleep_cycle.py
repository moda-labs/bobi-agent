"""Sleep-cycle harness (#456).

The sleep cycle monitor distills new agent transcripts into the team's single,
capped, rewritten-in-place ``long_term_memory.md``. This module holds the *deterministic*
half — windowing, the per-run input cap, oversized-message truncation, cursor
read/advance, transcript rendering, and the JSON-summary contract. The
*judgment* half (what is durable, how to file Facts vs Decisions, the actual
rewrite) lives in the sleep cycle agent's prompt (``bobi/prompts/sleep_cycle.md``)
and is exercised by the integration test, not here.

Splitting it this way keeps every silent-skip / cap / cursor invariant in plain,
unit-testable Python (the #454 lesson: never let a mocked model bypass the gate)
while leaving the distillation to the model where it belongs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Per-run input cap — the sleep cycle reads ALL new messages across ALL sessions
# each interval, and that input is large and variable-cost (the director alone
# is huge). This bounds what one run ingests; the higher-id overflow defers to
# the next run (see select_messages). Distinct from MAX_MEMORY_CHARS, which caps
# the *output* document. A char budget, not a token budget — cheap to compute
# deterministically and good enough to bound ingest cost.
MAX_SLEEP_CYCLE_INPUT_CHARS = 200_000

# The one-time seed (impl step 7) distills the full existing INDEX.md journal(s)
# in one shot and must NOT be clipped by the per-run cap — capping the seed would
# truncate the very knowledge it exists to preserve. A generous one-shot budget.
MAX_SEED_INPUT_CHARS = 2_000_000

# Reference memory can grow beyond the injected hot-memory budget. Keep the
# sleep-cycle task bounded; the file remains available on disk for exact edits.
MAX_REFERENCE_INPUT_CHARS = 80_000

# Marker left in a force-truncated oversized message so the lossy edit is visible
# in the rendered transcript the sleep cycle reads.
_ELISION = "\n… [truncated {n} chars] …\n"

_REFERENCE_ELISION = (
    "\n\n[reference preview truncated; read workspace/memory/reference.md "
    "before editing]\n"
)


def read_cursor(cursor_path: Path) -> int:
    """Read the sleep cycle's consumption watermark (a ``messages.id``).

    Returns 0 (read everything) when the file is absent or unparseable — a
    fresh sleep_cycle starts from the beginning, never silently skips.
    """
    try:
        if not cursor_path.is_file():
            return 0
        return int(cursor_path.read_text().strip() or "0")
    except (OSError, ValueError):
        log.warning("Unreadable policy cursor at %s — restarting from 0", cursor_path)
        return 0


def write_cursor(cursor_path: Path, cursor: int) -> None:
    """Advance the sleep cycle's watermark to ``cursor`` (the highest ingested id).

    Called only after a successful rewrite, so a run that dies mid-distillation
    leaves the cursor unmoved and the next run re-reads the same window.
    """
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(str(int(cursor)))


def _row_text(row: dict) -> str:
    """Render one indexed message row to the text the sleep cycle reads (and the
    text we size against the input budget)."""
    content = row.get("content") or ""
    tool = row.get("tool_name") or ""
    if tool:
        tool_input = row.get("tool_input") or ""
        body = f"[tool:{tool}] {tool_input}".strip()
        return f"{content}\n{body}".strip() if content else body
    return content


def _truncate_head_tail(text: str, budget: int) -> str:
    """Head+tail slice of ``text`` fitting in ``budget`` chars, with an explicit
    elision marker naming how many chars were dropped — lossy and LOUD."""
    if len(text) <= budget:
        return text
    dropped = len(text) - budget
    marker = _ELISION.format(n=dropped)
    keep = max(budget - len(marker), 0)
    head = keep // 2
    tail = keep - head
    if tail:
        return text[:head] + marker + text[-tail:]
    return text[:head] + marker


def _reference_for_prompt(text: str) -> str:
    """Bound the reference preview embedded in the sleep-cycle task."""
    if len(text) <= MAX_REFERENCE_INPUT_CHARS:
        return text
    keep = max(MAX_REFERENCE_INPUT_CHARS - len(_REFERENCE_ELISION), 0)
    return text[:keep] + _REFERENCE_ELISION


def select_messages(rows: list[dict], max_chars: int) -> tuple[list[dict], int | None, dict]:
    """Apply the per-run input cap to ``rows`` (oldest-first by id).

    Ingests oldest-first up to ``max_chars`` and DEFERS the higher-id overflow to
    the next run — never drops it, because the cursor advances only to the top of
    the contiguous ingested block, so the deferred tail sits above the new cursor
    and is re-read. The one exception is an oversized *oldest-unread* message that
    alone exceeds the budget: it is head+tail truncated (so the cursor can move
    past it and never permanently stall), ingested, and flagged loudly.

    Returns ``(ingested, highest_ingested_id, flags)`` where ``ingested`` are the
    (possibly truncated) rows to render, ``highest_ingested_id`` is the cursor's
    new value (None when nothing was ingested), and ``flags`` carries
    ``input_truncated`` / ``deferred_id_range`` / ``oversized_truncated`` /
    ``oversized_ids`` for the change summary.
    """
    ordered = sorted(rows, key=lambda r: r["id"])
    ingested: list[dict] = []
    used = 0
    highest: int | None = None
    oversized_ids: list[int] = []
    deferred = False

    for row in ordered:
        text = _row_text(row)
        size = len(text)

        # Oldest unread message alone exceeds the budget: truncate-to-fit so the
        # watermark can advance past it. Only ever the first message of a run.
        if not ingested and size > max_chars:
            trow = dict(row)
            trow["content"] = _truncate_head_tail(text, max_chars)
            trow["tool_name"] = ""  # the truncated text already inlines any tool body
            trow["tool_input"] = ""
            ingested.append(trow)
            used = len(trow["content"])
            highest = row["id"]
            oversized_ids.append(row["id"])
            continue

        if used + size > max_chars:
            deferred = True
            break

        ingested.append(row)
        used += size
        highest = row["id"]

    deferred_rows = ordered[len(ingested):] if deferred else []
    flags = {
        "input_truncated": bool(deferred_rows),
        "deferred_id_range": (
            (deferred_rows[0]["id"], deferred_rows[-1]["id"]) if deferred_rows else None
        ),
        "oversized_truncated": len(oversized_ids),
        "oversized_ids": oversized_ids,
    }
    return ingested, highest, flags


def render_transcript(rows: list[dict]) -> str:
    """Render ingested rows into a per-session-grouped transcript for the
    sleep cycle prompt. Preserves id order within each session."""
    by_session: dict[str, list[dict]] = {}
    for row in rows:
        by_session.setdefault(row.get("session_id", "?"), []).append(row)

    blocks: list[str] = []
    for session_id, msgs in by_session.items():
        lines = [f"### session: {session_id}"]
        for m in msgs:
            who = m.get("role") or m.get("type") or "?"
            text = _row_text(m).strip()
            if text:
                lines.append(f"[{who} #{m['id']}] {text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_sleep_cycle_task(prompt_template: str, transcript: str,
                       current_policy: str, flags: dict, seed: str = "",
                       current_reference: str = "") -> str:
    """Assemble the sleep cycle agent's task: the dedicated sleep cycle prompt, the
    current long_term_memory.md, and the rendered transcript delta. The agent does the
    judgment and rewrites long_term_memory.md via Write, then prints the JSON summary.

    ``seed`` (one-time, first run only) carries the legacy per-session decision
    logs to distill into the first long_term_memory.md — see scheduler._spawn_sleep_cycle.
    """
    notes = []
    if flags.get("input_truncated"):
        lo, hi = flags.get("deferred_id_range") or (None, None)
        notes.append(f"- Per-run input cap hit: messages id {lo}–{hi} were DEFERRED "
                     f"to the next run (set input_truncated=true and name this range).")
    if flags.get("oversized_truncated"):
        notes.append(f"- {flags['oversized_truncated']} oversized message(s) were "
                     f"head+tail truncated to fit (ids {flags.get('oversized_ids')}); "
                     f"set oversized_truncated and name them.")
    if flags.get("memory_over_budget"):
        observed = flags.get("memory_chars")
        budget = flags.get("memory_budget")
        cap = flags.get("memory_cap")
        if flags.get("memory_over_cap"):
            notes.append(f"- Current long_term_memory.md exceeds the {cap}-character cap "
                         f"({observed} chars) and the {budget}-character working "
                         f"budget. This is a compaction-required run: rewrite "
                         f"long_term_memory.md under the working budget even if there "
                         f"are no new transcript messages.")
        else:
            notes.append(f"- Current long_term_memory.md exceeds the {budget}-character "
                         f"working budget ({observed} chars; hard cap {cap}). This is "
                         f"a compaction-required run: rewrite long_term_memory.md "
                         f"under the working budget even if there are no new transcript "
                         f"messages.")
    notes_block = ("\n\nIngest notes (from the deterministic input cap):\n"
                   + "\n".join(notes)) if notes else ""

    seed_block = (
        f"\n\n=== ONE-TIME SEED: legacy decision-log journals ===\n"
        f"This is the first sleep cycle run — there is no long_term_memory.md yet. The blocks "
        f"below are the team's existing append-only decision logs. Distill their "
        f"DURABLE facts and decisions into the first long_term_memory.md (same rules as the "
        f"transcript delta: dedup, generalize, drop one-off operational detail). "
        f"After this run the journals are never read again.\n\n{seed}"
    ) if seed else ""

    return (
        f"{prompt_template}\n\n"
        f"=== CURRENT long_term_memory.md (rewrite this in full via Write) ===\n"
        f"{current_policy or '(empty — no long_term_memory.md yet)'}\n\n"
        f"=== CURRENT workspace/memory/reference.md (update in place if needed) ===\n"
        f"{_reference_for_prompt(current_reference) or '(empty — no reference.md yet)'}\n\n"
        f"=== NEW TRANSCRIPT DELTA (since your last run) ===\n"
        f"{transcript or '(no new messages)'}"
        f"{notes_block}"
        f"{seed_block}"
    )


def parse_result(output: str) -> dict | None:
    """Extract the trailing JSON summary the sleep cycle agent printed, or None.

    Mirrors scheduler._parse_verdict but keys on the sleep_cycle contract: a dict
    carrying ``success``. None means no parseable summary — an indeterminate
    run, which must NOT advance the cursor (treated as failure by the caller).
    """
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "success" in parsed:
            return parsed
    return None
