"""Recover token telemetry for sessions recorded before the #935 fix.

Bobi read the Claude SDK's per-model usage under its legacy snake_case
spellings, so every token counter persisted as zero while provider dollars
still arrived. The forward fix lives in ``bobi.brain.claude``; this module
repairs the history it left behind, from the one durable record that still
holds the numbers - Claude's retained JSONL transcripts.

It only ever FILLS. A session whose ``model_usage`` already carries token
volume is left exactly as recorded, provider dollars are never touched, and a
session whose transcript is gone stays honestly unknown rather than estimated
into place.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from bobi.sdk import SessionRegistry

# The provider a Claude transcript can speak for. A session recorded against
# any other provider is skipped outright: its tokens were never Claude's.
CLAUDE_PROVIDER = "anthropic"


@dataclass
class BackfillReport:
    """What one backfill pass found, per session."""

    repaired: int = 0
    already_populated: int = 0
    transcript_missing: int = 0
    unparseable: int = 0
    skipped: int = 0
    repaired_sessions: list[str] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return (self.repaired + self.already_populated
                + self.transcript_missing + self.unparseable + self.skipped)

    def render(self, *, write: bool) -> str:
        verb = "Repaired" if write else "Would repair"
        return "\n".join([
            f"Sessions scanned:     {self.scanned}",
            f"{verb + ':':<22}{self.repaired}",
            f"Already populated:    {self.already_populated}",
            f"Transcript missing:   {self.transcript_missing}",
            f"Transcript unusable:  {self.unparseable}",
            f"Skipped:              {self.skipped}",
        ])


def claude_projects_dirs(claude_config_dir: Path | None = None) -> list[Path]:
    """Where Claude retains transcripts, most specific first."""
    if claude_config_dir is not None:
        return [Path(claude_config_dir) / "projects"]
    dirs = []
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        dirs.append(Path(configured) / "projects")
    home = Path.home() / ".claude" / "projects"
    if home not in dirs:
        dirs.append(home)
    return dirs


def index_transcripts(claude_config_dir: Path | None = None) -> dict[str, Path]:
    """Map Claude session id -> transcript path, walked once.

    Transcripts are named ``<session_id>.jsonl`` under a per-project directory
    whose name is a slug of the session's cwd. The slug rule is Claude's, not
    ours, so a session's ``cwd`` only orders the search (see
    :func:`resolve_transcript`) - the uuid filename is what actually resolves.
    """
    index: dict[str, Path] = {}
    for projects in claude_projects_dirs(claude_config_dir):
        if not projects.is_dir():
            continue
        try:
            project_dirs = sorted(projects.iterdir())
        except OSError:
            continue
        for project_dir in project_dirs:
            if not project_dir.is_dir():
                continue
            try:
                transcripts = sorted(project_dir.glob("*.jsonl"))
            except OSError:
                continue
            for path in transcripts:
                # First projects dir wins: an explicit --claude-config-dir or
                # CLAUDE_CONFIG_DIR outranks the home default.
                index.setdefault(path.stem, path)
    return index


def resolve_transcript(session_id: str, cwd: str,
                       index: dict[str, Path]) -> Path | None:
    """The retained transcript for *session_id*, preferring *cwd*'s project.

    Claude session ids are uuids, so the index lookup is already unambiguous;
    ``cwd`` is used only to break a tie if two roots ever retained the same id.
    """
    if not session_id:
        return None
    path = index.get(session_id)
    if path is None or not cwd:
        return path
    slug = _project_slug(cwd)
    sibling = path.parent.parent / slug / f"{session_id}.jsonl"
    return sibling if sibling.exists() else path


def _project_slug(cwd: str) -> str:
    """Claude's project-directory name for a working directory."""
    return "".join(c if c.isalnum() or c == "-" else "-" for c in str(cwd))


def transcript_usage(path: Path) -> dict[str, dict[str, int]]:
    """Token usage in one transcript, summed per model.

    Claude writes one ``assistant`` line per content block of a single API
    response, each repeating that response's usage verbatim, so lines are
    deduped by message id - summing them raw overcounts a real transcript
    roughly twofold. Sidechain (Task subagent) lines are counted: that is real
    spend on the same session.

    Raises ``OSError`` if the transcript cannot be read; unreadable individual
    lines are skipped, which is how a truncated tail stays recoverable.
    """
    per_model: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    with path.open() as fh:
        for line in fh:
            record = _parse_line(line)
            if record is None:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            model = message.get("model")
            if not isinstance(usage, dict) or not isinstance(model, str):
                continue
            key = (message.get("id") or record.get("requestId")
                   or record.get("uuid"))
            if not isinstance(key, str) or not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            cache_read = _tokens(usage, "cache_read_input_tokens")
            totals = per_model.setdefault(model, {
                "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0,
            })
            # Same composition the live path records: the input volume is the
            # whole context (uncached + cache creation + cache read), with the
            # cache read kept split out for the per-model discount.
            totals["input_tokens"] += (
                _tokens(usage, "input_tokens")
                + _tokens(usage, "cache_creation_input_tokens")
                + cache_read
            )
            totals["cached_input_tokens"] += cache_read
            totals["output_tokens"] += _tokens(usage, "output_tokens")
    # Claude records locally-generated turns (interrupts, API-error
    # placeholders) under the pseudo-model ``<synthetic>`` with no usage.
    # Recording a model that spent nothing would invent a permanently
    # zero-token entry and make every later pass re-repair it.
    return {model: totals for model, totals in per_model.items()
            if totals["input_tokens"] or totals["output_tokens"]}


def _parse_line(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    return record


def _tokens(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def backfill_usage(*, claude_config_dir: Path | None = None,
                   write: bool = False,
                   root: Path | None = None) -> BackfillReport:
    """Fill missing token telemetry across one team's session registry.

    ``write=False`` (the default) resolves and parses every transcript and
    reports exactly what it would repair, touching nothing.
    """
    report = BackfillReport()
    registry = SessionRegistry(root)
    index = index_transcripts(claude_config_dir)

    claimed: set[str] = set()
    for entry in _entries_oldest_first(registry):
        provider = (entry.provider or "").strip()
        if provider and provider != CLAUDE_PROVIDER:
            report.skipped += 1
            continue
        # One transcript's tokens belong to one run. Two registry rows sharing
        # a session id (a resumed or re-registered run) must not each book the
        # same spend.
        if entry.session_id and entry.session_id in claimed:
            report.skipped += 1
            continue

        transcript = resolve_transcript(entry.session_id, entry.cwd, index)
        if transcript is None:
            report.transcript_missing += 1
            continue
        try:
            usage = transcript_usage(transcript)
        except OSError:
            report.unparseable += 1
            continue
        if not usage:
            report.unparseable += 1
            continue

        outcome = registry.backfill_usage(
            entry.name, usage, provider=CLAUDE_PROVIDER, write=write,
        )
        if outcome == "repaired":
            report.repaired += 1
            report.repaired_sessions.append(entry.name)
            claimed.add(entry.session_id)
        elif outcome == "already_populated":
            report.already_populated += 1
            claimed.add(entry.session_id)
        else:
            # The state file vanished between the listing and the write.
            report.skipped += 1
    return report


def _entries_oldest_first(registry: SessionRegistry) -> list:
    """Registry entries oldest run first, ties broken by name.

    Deterministic ordering is what makes a shared-session-id tie break the same
    way on every pass, so the backfill stays idempotent.
    """
    return sorted(registry.list_all(), key=lambda e: (e.started_at, e.name))
