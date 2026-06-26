"""Minimal web UI for a *running* bobi team — cards per active agent,
click-to-chat. See :mod:`bobi.agentui.server`."""

from __future__ import annotations

from pathlib import Path


def run_ui(project_path: Path, *, open_browser: bool = True) -> int:
    """Foreground entry point for the `bobi ui` command."""
    from bobi.agentui import server
    return server.serve(project_path, mode="local", open_browser=open_browser)
