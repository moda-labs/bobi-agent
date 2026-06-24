"""Minimal web UI for a *running* modastack team — cards per active agent,
click-to-chat. See :mod:`modastack.agentui.server`."""

from __future__ import annotations

from pathlib import Path


def run_ui(project_path: Path, *, open_browser: bool = True) -> int:
    """Foreground entry point for the `modastack ui` command."""
    from modastack.agentui import server
    return server.serve(project_path, mode="local", open_browser=open_browser)
