"""Interactive agent-guided onboarding (`bobi agents setup`).

A local web UI (FastAPI on 127.0.0.1, foreground) drives a mode-aware
stage machine; the wizard owns navigation and every deterministic action
(see state.py, actions.py), while the LLM serves the digestion brain
(conversation → spec) and the Build pour (spec → pack files). See
`webui/server.py` for the app and launcher.

Setup creates or resumes one named Bobi Agent under BOBI_HOME. The
editable source defaults to <BOBI_HOME>/agents/<name>/src, while the
installed runtime lives at <BOBI_HOME>/agents/<name>/run.
"""

from __future__ import annotations

from pathlib import Path


def run_setup(project_path: Path, model: str | None = None,
              resume: bool = False) -> int:
    """Launch the local web UI for setup. Returns a process exit code."""
    from bobi.setup.webui.server import serve

    return serve(project_path, model=model, resume=resume)
