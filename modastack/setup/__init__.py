"""Interactive agent-guided onboarding (`modastack setup`).

One persistent Claude session drives the conversation with the user at
the terminal; the setup state machine is enforced by in-process tools
that refuse out-of-order calls (see state.py, tools.py). Python owns
the REPL loop and every deterministic action: registry listing,
credential capture, venn checks, validation, install, preflight.

Like `modastack install`, setup targets its literal cwd — it CREATES
the installation root, so it never calls paths.resolve_root().
"""

from __future__ import annotations

from pathlib import Path


def run_setup(project_path: Path, model: str | None = None,
              resume: bool = False) -> int:
    """Run the interactive setup session. Returns a process exit code."""
    import asyncio

    from modastack.setup.repl import run_repl

    return asyncio.run(run_repl(project_path, model=model, resume=resume))
