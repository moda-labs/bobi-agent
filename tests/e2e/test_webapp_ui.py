"""End-to-end browser tests for the `bobi app` web UI (Playwright).

Sibling of `test_setup_ui.py`, over the other local surface. The setup suite
drives one project's onboarding wizard; this one drives the machine-scoped
app — the dashboard of every installed agent, and the single-agent page
(status band, runs table, run modal, write actions).

Everything is real: a seeded `$BOBI_HOME` on disk, `bobi.webapp.server`'s
FastAPI app booted on a loopback port, and Chromium driven through the same
token + Host-guard path the CLI-launched UI uses. The read models fold real
session / workflow / monitor records, seeded with the same writers
`tests/test_webapp_runs.py` proves them against.

Skips cleanly when Playwright isn't installed (the unit job doesn't need it).
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_webapp_runs import _monitor, _session, _workflow  # noqa: E402


def _open(page, url):
    """Load a route and wait for the shell's first paint."""
    page.goto(url)
    page.wait_for_selector(".bar .lockup")
    return page


class TestShell:
    def test_dashboard_lists_the_installed_agent(self, webapp, page):
        _open(page, webapp.url)
        tile = page.locator(".agent-tile", has_text=webapp.agent)
        tile.wait_for()
        assert tile.locator(".agent-name").inner_text() == webapp.agent
