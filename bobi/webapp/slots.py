"""Slot identity on disk — a slot IS its team (#526: `agents/<name>/`).

Hosted onboarding opens a slot under whatever name the user typed, but the
team gets its real name during setup: a template pick, an auto-name, an
explicit rename. Finishing therefore has to move the slot to match, and that
move carries enough rules — an already-occupied target, a `run/` directory
worth salvaging, a source that must actually belong to the target — to be
worth naming and testing on its own, rather than living as a closure inside
an HTTP handler where the only way to reach it is a full request plus a
SetupState.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from bobi import paths
from bobi.chat_history import safe_name


def finalize_slot(placeholder: str, final: str, source_dir: str) -> None:
    """Move slot *placeholder* to *final* now the team has its real name.

    Best-effort by design: every rename this cannot perform safely leaves the
    tree exactly as it was rather than raising, because the caller has already
    finished onboarding and a tidy-up failure must not fail the request.
    Nothing is running at this point (finish no longer launches) and the setup
    session is released, so the move races nothing.

    *source_dir* is the setup state's recorded source. It is what distinguishes
    an occupied target that is the SAME team — in which case this slot's `run/`
    belongs in it and the empty placeholder goes away — from a different team
    that happens to hold the name, which is left strictly alone.
    """
    # `safe_name` is a path-traversal guard, not a name validator — "   "
    # passes it — so the strip happens here rather than being a rule every
    # caller has to remember.
    final = (final or "").strip()
    if not final or final == placeholder or not safe_name(final):
        return
    old_dir = paths.agent_dir(placeholder)
    if not old_dir.is_dir():
        return

    new_dir = paths.agent_dir(final)
    if not new_dir.exists():
        shutil.move(str(old_dir), str(new_dir))
        return

    # The name is taken. Only fold into it when it is demonstrably this same
    # team — its source dir is the one setup has been editing.
    same_team = (
        paths.agent_source_dir(final).is_dir()
        and Path(source_dir or "").resolve()
        == paths.agent_source_dir(final).resolve()
    )
    if not same_team:
        return

    old_run = old_dir / "run"
    new_run = new_dir / "run"
    if old_run.is_dir() and not new_run.exists():
        shutil.move(str(old_run), str(new_run))
    try:
        old_dir.rmdir()
    except OSError:
        pass
