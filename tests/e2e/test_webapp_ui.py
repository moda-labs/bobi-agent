"""End-to-end browser tests for the `bobi app` web UI (Playwright).

Sibling of `test_setup_ui.py`, over the other local surface. That suite drives
one project's onboarding wizard; this one drives the machine-scoped app. Two
surfaces: the dashboard of every installed agent, and the single-agent page
(status band, telemetry, the one runs table, the run modal, both write
actions).

Everything is real. A seeded `$BOBI_HOME` on disk, `bobi.webapp.server`'s
FastAPI app booted on a loopback port, and Chromium driven through the same
token + Host-guard path the CLI-launched UI uses. The read models fold real
session / workflow / monitor records, written with the same helpers
`tests/test_webapp_runs.py` proves those folds against, and the state tri-state
comes from a manager pid that is genuinely alive.

Two things are driven from the browser side instead, and only two, because
their success needs something this process cannot have offline: a Slack post
(`remind`) and a server that fails a read on demand (the `runsError` branch).
Both have an unstubbed companion test alongside them.

Skips cleanly when Playwright isn't installed (the unit job doesn't need it).
"""

from __future__ import annotations

import json
import os
import re
import shutil

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect  # noqa: E402

from tests.test_webapp_runs import NOW, _monitor, _session, _workflow  # noqa: E402

RUNS_URL = re.compile(r"/api/agents/[^/]+/runs\?")
DETAILS_URL = re.compile(r"/api/agents/[^/]+/runs/[^/]+/details")
REMIND_URL = re.compile(r"/api/agents/[^/]+/workflows/runs/[^/]+/remind")

# The runs table pages at 100. One row past it is the whole pager contract.
PAGE_SIZE = 100


# --- opening a route --------------------------------------------------------

def _dashboard(page, webapp):
    page.goto(webapp.url)
    page.wait_for_selector(".agent-tile")
    return page


def _agent(page, webapp, name=None):
    page.goto(webapp.agent_url(name))
    page.wait_for_selector(".agent-page")
    return page


# --- seeding ----------------------------------------------------------------

def _seed_runs(install):
    """One row of each shape the table has to render.

    Timestamps come from `test_webapp_runs`' fixed NOW, which is comfortably
    more than a day in the past, so the waiting workflow really has sat past
    `AWAITING_ACTION_AFTER_SECONDS` and is elevated by the same clock
    comparison production uses, not by a status word written by hand.
    """
    _session(install, "worker-a", status="completed", title="Fix the flaky test",
             model_usage={"sonnet-5": {"input_tokens": 1200,
                                       "output_tokens": 800}},
             total_cost_usd=1.25)
    _session(install, "worker-b", status="failed", title="Ship the migration",
             error="the migration never applied")
    _workflow(install, "wf-gate", status="waiting", name="adhoc",
              started_at=NOW - 90000, completed_at=0,
              suspended_at_step=1, await_event="human_approval")
    _monitor(install, "inbox-watch")


def _seed_transcript(webapp, monkeypatch, session, lines):
    """Record a real Claude transcript where the reader actually looks.

    `LocalRuntime.transcript` resolves the session's recorded id
    (`state/sessions/<name>.id`) and then hunts for `<id>.jsonl` under the
    Claude config dirs. Pointing `CLAUDE_CONFIG_DIR` at a temp tree and writing
    the file there drives that whole path with nothing patched.
    """
    session_id = "e2e-" + session
    (webapp.install.sessions_dir / f"{session}.id").write_text(session_id)
    config = webapp.install.repo_path.parent / "claude"
    project = config / "projects" / "e2e-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))


def _entry(kind, content, at):
    return json.dumps({"type": kind, "timestamp": at,
                       "message": {"content": content}})


def _record_requests(page, pattern=None, method=""):
    """Collect the URLs the page requests from now on, as a live list."""
    seen = []

    def note(request):
        if method and request.method != method:
            return
        if pattern is None or pattern.search(request.url):
            seen.append(request.url)

    page.on("request", note)
    return seen


def _stub_claude(tmp_path, monkeypatch):
    """Put a `claude` on PATH.

    `/api/setup/open` refuses to start onboarding without the Claude Code CLI,
    which the e2e container does not ship. The check is `shutil.which`, so the
    honest way to test the create tile's routing is to satisfy exactly that.
    Opening a session never invokes the CLI.
    """
    bindir = tmp_path / "claude-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


# --- shell and routing ------------------------------------------------------

class TestShell:
    def test_dashboard_lists_the_installed_agent(self, webapp, page):
        _dashboard(page, webapp)
        tile = page.locator(".agent-tile", has_text=webapp.agent)
        expect(tile.locator(".agent-name")).to_have_text(webapp.agent)
        # The card's badge is `healthChip()`: nothing is running under a fresh
        # seeded home, and an installed team that is not running is "stopped".
        expect(tile.locator(".status")).to_have_text("stopped")
        expect(page.locator(".agent-tile.create")).to_be_visible()

    def test_create_tile_routes_into_setup(self, webapp, page, tmp_path,
                                           monkeypatch):
        _stub_claude(tmp_path, monkeypatch)
        _dashboard(page, webapp)
        page.locator(".agent-tile.create").click()
        # The tile opens a real onboarding slot and hands the browser to it.
        page.wait_for_url(re.compile(r"/setup/new-agent/"))

    def test_agent_tile_opens_the_agent_view(self, webapp, page):
        _dashboard(page, webapp)
        page.locator(".agent-tile", has_text=webapp.agent).click()
        page.wait_for_selector(".agent-page")
        expect(page.locator(".agent-page-header h1")).to_have_text(webapp.agent)

    def test_subtitle_tracks_the_route(self, webapp, page):
        # `setSubtitle` has no null guard and every route calls it, so this is
        # also the assertion that catches #subtitle being removed at all.
        _dashboard(page, webapp)
        expect(page.locator("#subtitle")).to_have_text("agents")

        _agent(page, webapp)
        expect(page.locator("#subtitle")).to_have_text(webapp.agent)

    def test_the_back_slot_appears_on_the_agent_view_and_returns(
            self, webapp, page):
        _dashboard(page, webapp)
        # The dashboard IS the top of the tree; there is nowhere to go back to.
        expect(page.locator(".bar .navback")).to_be_empty()

        _agent(page, webapp)
        back = page.locator(".bar .navback .navback-link")
        expect(back).to_have_text("← agents")
        back.click()
        page.wait_for_selector(".agent-tile")
        expect(page.locator("#subtitle")).to_have_text("agents")

    def test_the_health_dot_goes_stale_then_gone_when_the_server_dies(
            self, webapp, page):
        _dashboard(page, webapp)
        expect(page.locator("#health")).to_have_class("dot")
        expect(page.locator("#gone")).to_be_hidden()

        webapp.stop()
        # One poll tick fails two reads, so the dot goes stale a full interval
        # before `noteFailure` reaches its third strike and unhides the
        # overlay. Both halves of that staging are asserted: a single failure
        # must not black out the page.
        expect(page.locator("#health")).to_have_class("dot stale")
        expect(page.locator("#gone")).to_be_visible(timeout=20_000)
        expect(page.locator("#gone")).to_contain_text("bobi app start")


# --- the status band and lifecycle -----------------------------------------

class TestStatusBand:
    @pytest.mark.parametrize("state,word,cls", [
        ("stopped", "stopped", "stopped"),
        ("running", "running", "running"),
        ("not_responding", "not responding", "failed"),
    ])
    def test_the_badge_renders_the_state(self, webapp, page, state, word, cls):
        if state != "stopped":
            webapp.run_manager(responsive=state == "running")
        _agent(page, webapp)
        badge = page.locator(".agent-header-state .status-badge")
        expect(badge).to_have_text(word)
        expect(badge).to_have_class(f"status-badge {cls}")

    @pytest.mark.parametrize("state,labels", [
        ("stopped", ["Start agent"]),
        ("running", ["Restart", "Stop"]),
        ("not_responding", ["Restart agent"]),
    ])
    def test_the_actions_match_the_state(self, webapp, page, state, labels):
        if state != "stopped":
            webapp.run_manager(responsive=state == "running")
        _agent(page, webapp)
        expect(page.locator(".agent-header-actions button")).to_have_text(labels)

    def test_a_failed_start_surfaces_its_preflight_report(self, webapp, page):
        # A team whose entry-point role is missing fails preflight, so `start`
        # answers 409 with the report and never spawns anything.
        shutil.rmtree(webapp.install.repo_path / "package" / "roles" / "director")

        _agent(page, webapp)
        page.locator(".agent-header-actions button").click()
        report = page.locator(".band-report")
        expect(report).to_be_visible()
        expect(report.locator(".rep-head")).to_have_text("start failed")
        expect(report).to_contain_text("role 'director' not found")
        # The strip recovers: the button is live again, not stuck on "starting…".
        expect(page.locator(".agent-header-actions button")).to_have_text(
            "Start agent")

    def test_telemetry_renders_segments_and_hides_when_there_are_none(
            self, webapp, page):
        _agent(page, webapp)
        # A stopped agent that never ran has no terminal record, so the strip
        # has nothing honest to show and the grid stays away entirely.
        expect(page.locator(".telemetry-grid")).to_be_hidden()

        pid = webapp.run_manager(responsive=True)
        grid = page.locator(".telemetry-grid")
        expect(grid).to_be_visible(timeout=20_000)
        expect(grid.locator(".metric-tile .metric-label")).to_have_text(
            ["Manager pid", "Live runs"])
        expect(grid.locator(".metric-tile .metric-value").first).to_have_text(
            str(pid))


# --- the runs table ---------------------------------------------------------

class TestRunsTable:
    def test_rows_render_status_title_when_and_cost(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)

        row = page.locator(".runs tbody tr", has_text="Fix the flaky test")
        expect(row).to_be_visible()
        expect(row.locator(".rstat")).to_have_class("rstat done")
        expect(row.locator(".rstat span:not(.rdot)")).to_have_text("Done")
        expect(row.locator(".r-title")).to_have_text("Fix the flaky test")
        # Server sends raw epochs and seconds; the browser owns the formatting.
        expect(row.locator(".r-when .dur")).to_have_text("5m")
        expect(row.locator(".r-tok")).to_have_text("2.0K tok · $1.25")

        failed = page.locator(".runs tbody tr", has_text="Ship the migration")
        expect(failed.locator(".r-note")).to_have_class("r-note bad")
        expect(failed.locator(".r-note")).to_have_text(
            "the migration never applied")

    def test_tabs_filter_and_carry_counts(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)

        tabs = page.locator(".tabs .tab")
        # `all` stays bare on purpose: the section count beside the table is
        # already the all-count, and printing it twice reads as two facts.
        expect(tabs).to_have_text(
            ["all", "running · 0", "awaiting action · 1", "failed · 1"])
        expect(page.locator("[data-el=runsCount]")).to_have_text("4")

        page.locator(".tabs .tab", has_text="failed").click()
        expect(page.locator(".runs tbody tr")).to_have_count(1)
        expect(page.locator(".runs tbody tr .r-title")).to_have_text(
            "Ship the migration")
        # Counts describe the whole set, not the filtered page.
        expect(tabs).to_have_text(
            ["all", "running · 0", "awaiting action · 1", "failed · 1"])

    def test_search_filters_the_table(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)
        expect(page.locator(".runs tbody tr")).to_have_count(4)

        # Typed a key at a time, as a person does. The 250ms debounce is the
        # difference between one read and nine, so the count of searching
        # reads is the assertion, not a comment claiming there is a debounce.
        searches = _record_requests(page, re.compile(r"/runs\?.*query="))
        page.locator(".runs-search").press_sequentially("migration", delay=25)
        expect(page.locator(".runs tbody tr")).to_have_count(1)
        assert len(searches) == 1, searches

        expect(page.locator(".runs tbody tr .r-title")).to_have_text(
            "Ship the migration")
        expect(page.locator(".pager-summary")).to_have_text("1–1 of 1 matches")

    def test_the_pager_summarises_and_disables_at_the_ends(self, webapp, page):
        for i in range(PAGE_SIZE + 1):
            _session(webapp.install, f"worker-{i:03d}", title=f"Run {i:03d}",
                     started_at=NOW - 600 - i, terminal_at=NOW - 300 - i)
        _agent(page, webapp)

        expect(page.locator(".pager-summary")).to_have_text(
            f"1–{PAGE_SIZE} of {PAGE_SIZE + 1}")
        expect(page.locator(".pager-page")).to_have_text("1 / 2")
        previous = page.locator(".runs-pager button", has_text="Previous")
        following = page.locator(".runs-pager button", has_text="Next")
        expect(previous).to_be_disabled()
        expect(following).to_be_enabled()

        following.click()
        expect(page.locator(".pager-summary")).to_have_text(
            f"{PAGE_SIZE + 1}–{PAGE_SIZE + 1} of {PAGE_SIZE + 1}")
        expect(page.locator(".pager-page")).to_have_text("2 / 2")
        expect(following).to_be_disabled()
        expect(previous).to_be_enabled()

        # A new query is a new result set, so the window has to go back to
        # the start or the table shows page 2 of something one row long.
        page.locator(".runs-search").fill("Run 007")
        expect(page.locator(".pager-page")).to_have_text("1 / 1")
        expect(page.locator(".runs tbody tr")).to_have_count(1)

    @pytest.mark.parametrize("tab,message", [
        (None, "No runs yet. Start the agent and its first work will appear "
               "here."),
        ("running", "No live runs."),
        ("awaiting action",
         "No workflows are waiting for approval or clarification."),
        ("failed", "No failed or crashed runs."),
    ])
    def test_empty_states_name_their_cause(self, webapp, page, tab, message):
        _agent(page, webapp)
        if tab:
            page.locator(".tabs .tab", has_text=tab).click()
        expect(page.locator(".runs-empty")).to_have_text(message)

    def test_no_match_says_so_rather_than_no_runs(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)
        page.locator(".runs-search").fill("nothing matches this")
        expect(page.locator(".runs-empty")).to_have_text(
            "No runs match “nothing matches this”.")

    def test_a_failed_read_stops_claiming_it_is_loading(self, webapp, page):
        # The rule this pins is in the source, not cosmetics: a read that
        # FAILED is not a read that is still running, and leaving "Loading…"
        # up promises work that is never coming. Only the server can answer
        # 500 on demand, so the failure is injected at the wire.
        page.route(RUNS_URL, lambda route: route.fulfill(
            status=500, content_type="application/json",
            body='{"error": "boom"}'))
        _agent(page, webapp)
        empty = page.locator(".runs-empty")
        expect(empty).to_have_text("Could not read this agent's runs.")

    def test_a_lost_server_says_the_table_stopped_updating(self, webapp, page):
        page.route(RUNS_URL, lambda route: route.abort())
        _agent(page, webapp)
        expect(page.locator(".runs-empty")).to_have_text(
            "Lost the app server — the table stopped updating.")


# --- the run modal ----------------------------------------------------------

class TestRunModal:
    def test_a_row_with_a_session_opens_its_transcript(self, webapp, page,
                                                       monkeypatch):
        _seed_runs(webapp.install)
        _seed_transcript(webapp, monkeypatch, "worker-a", [
            _entry("user", "fix the flaky test", "2026-08-01T09:15:00.000Z"),
            _entry("assistant", [
                {"type": "text", "text": "found the race"},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "pytest -q tests/test_flaky.py"}},
            ], "2026-08-01T09:15:42.000Z"),
        ])
        _agent(page, webapp)

        page.locator(".runs tbody tr", has_text="Fix the flaky test").click()
        expect(page.locator(".modal-backdrop")).to_have_class(
            "modal-backdrop open")
        expect(page.locator("[data-el=slabKind]")).to_have_text("transcript")
        expect(page.locator("[data-el=slabTitle]")).to_have_text(
            "Fix the flaky test")

        lines = page.locator(".transcript .tr-line")
        expect(lines).to_have_count(3)
        expect(lines.nth(0).locator(".ts")).to_have_text("09:15:00")
        expect(lines.nth(0).locator(".who")).to_have_text("user")
        expect(lines.nth(0).locator(".txt")).to_have_text("fix the flaky test")
        expect(lines.nth(1).locator(".who")).to_have_text("agent")
        expect(lines.nth(1).locator(".txt")).to_have_text("found the race")
        # A tool call is its own line: the thing the chat view throws away.
        expect(lines.nth(2)).to_have_class("tr-line tool")
        expect(lines.nth(2).locator(".txt")).to_have_text(
            "Bash: pytest -q tests/test_flaky.py")

    def test_a_monitor_row_opens_details_from_the_endpoint(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)

        with page.expect_request(DETAILS_URL):
            page.locator(".runs tbody tr", has_text="inbox-watch").click()
        expect(page.locator("[data-el=slabKind]")).to_have_text("details")
        body = page.locator(".transcript")
        expect(body).to_contain_text("outcome")
        expect(body).to_contain_text("quiet")

    def test_a_session_less_workflow_row_renders_without_a_fetch(
            self, webapp, page):
        # Its row already carries the whole story (what step, what event,
        # how long), and the details endpoint only serves monitor records.
        _seed_runs(webapp.install)
        _agent(page, webapp)

        fetched = _record_requests(page, DETAILS_URL)
        page.locator(".runs tbody tr", has_text="adhoc").click()
        expect(page.locator("[data-el=slabKind]")).to_have_text("details")
        body = page.locator(".transcript")
        expect(body).to_contain_text("Awaiting action")
        expect(body).to_contain_text("human_approval")
        assert not fetched

    @pytest.mark.parametrize("how", ["button", "backdrop", "escape"])
    def test_the_modal_closes_three_ways(self, webapp, page, how):
        _seed_runs(webapp.install)
        _agent(page, webapp)
        backdrop = page.locator(".modal-backdrop")

        page.locator(".runs tbody tr", has_text="inbox-watch").click()
        expect(backdrop).to_have_class("modal-backdrop open")

        if how == "button":
            page.locator("[data-el=slabClose]").click()
        elif how == "backdrop":
            # The corner, not the centre: the centre is the modal itself, and
            # only a click that lands on the backdrop should dismiss.
            backdrop.click(position={"x": 4, "y": 4})
        else:
            page.keyboard.press("Escape")
        expect(backdrop).to_have_class("modal-backdrop")


# --- the write actions ------------------------------------------------------

class TestWriteActions:
    def _gate_row(self, page):
        return page.locator(".runs tbody tr", has_text="adhoc")

    def test_remind_reflects_sent_and_returns_to_remind(self, webapp, page):
        # A real reminder is a real Slack post, which this process cannot make
        # offline, so only the delivery is answered at the wire. What is under
        # test is the button's own lifecycle, which is browser-side entirely.
        page.route(REMIND_URL, lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok": true, "delivered": true}'))
        _seed_runs(webapp.install)
        _agent(page, webapp)

        remind = self._gate_row(page).locator("button.remind")
        remind.click()
        expect(remind).to_have_text("Sent")
        expect(remind).to_be_disabled()
        # It goes back on its own; a button stuck on "Sent" cannot be used again.
        expect(remind).to_have_text("Remind", timeout=10_000)
        expect(remind).to_be_enabled()

    def test_a_failed_remind_reports_and_restores_the_button(self, webapp, page):
        # Unstubbed: no Slack credentials, so the real delivery really fails.
        _seed_runs(webapp.install)
        _agent(page, webapp)

        remind = self._gate_row(page).locator("button.remind")
        remind.click()
        expect(page.locator(".band-report .rep-head")).to_have_text(
            "reminder failed")
        expect(remind).to_have_text("Remind")
        expect(remind).to_be_enabled()

    def test_close_asks_first_and_cancelling_posts_nothing(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)

        asked = []
        page.on("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        posted = _record_requests(page, re.compile(r"/close$"), method="POST")

        self._gate_row(page).locator("button", has_text="Close").click()
        assert asked and "Closing ends this workflow" in asked[0]

        # Past a full 4s poll cycle, so "nothing happened" is a fact rather
        # than a race won. Drop the `if (!confirmed) return` guard and the run
        # is closed inside this window and the row says so.
        page.wait_for_timeout(5000)
        assert not posted
        expect(self._gate_row(page).locator("button", has_text="Close")
               ).to_be_enabled()
        expect(self._gate_row(page).locator(".rstat span:not(.rdot)")
               ).to_have_text("Awaiting action")

    def test_close_confirmed_closes_the_run(self, webapp, page):
        _seed_runs(webapp.install)
        _agent(page, webapp)

        page.on("dialog", lambda d: d.accept())
        self._gate_row(page).locator("button", has_text="Close").click()
        expect(self._gate_row(page).locator(".rstat span:not(.rdot)")
               ).to_have_text("Closed", timeout=10_000)


# --- the agent that isn't there --------------------------------------------

class TestMissingAgent:
    def test_a_route_naming_an_uninstalled_agent_says_so(self, webapp, page):
        _agent(page, webapp, "not-installed-here")
        stub = page.locator(".agent-page .stub")
        expect(stub.locator("h2")).to_have_text("not-installed-here")
        expect(stub).to_contain_text(
            "No agent by that name is installed on this machine.")
        # An empty shell would instead offer a Start button for nothing.
        expect(page.locator(".agent-header-actions")).to_have_count(0)
        expect(stub.locator("a")).to_have_text("All agents")
        stub.locator("a").click()
        page.wait_for_selector(".agent-tile")


# --- the saved popover's window --------------------------------------------

class TestSavedPopover:
    """The window the figures cover (MOD-373).

    Every number in this card is lifetime-cumulative - `rollup_costs` folds
    each session's whole recorded cost with no time filter. Saying so is the
    difference between "saved ~$50" meaning this week and meaning since the
    first run, and the reader cannot tell them apart from the figures.
    """

    def _open(self, page, webapp):
        _agent(page, webapp)
        chip = page.locator('[data-el="savedChip"]')
        expect(chip).not_to_have_text("saved …", timeout=10_000)
        chip.click()
        card = page.locator('[data-el="savedCard"]')
        expect(card).to_be_visible()
        return card

    def test_the_eyebrow_states_the_figures_are_lifetime(self, webapp, page):
        _seed_runs(webapp.install)
        card = self._open(page, webapp)
        # Scoped on the heading, so it covers every row, not just the total.
        expect(card.locator(".eyebrow")).to_have_text("saved · lifetime")

    def test_the_window_is_stated_once_not_twice(self, webapp, page):
        """The note used to carry the only mention, buried in the estimate
        sentence. Now the eyebrow owns the window and the note owns the
        caveat - say either twice and neither reads as load-bearing."""
        _seed_runs(webapp.install)
        card = self._open(page, webapp)
        note = card.locator(".note")
        # The honest limit on "lifetime" survives; the duplicate does not.
        expect(note).to_contain_text("over runs still on disk")
        assert "lifetime" not in note.inner_text().lower()

    def test_the_chip_counts_a_single_run_in_the_singular(self, webapp, page):
        """`_seed_runs` leaves exactly one session with usage to fold, which
        the chip rendered as "1 runs" - the dashboard's session count next to
        it has always pluralised."""
        _seed_runs(webapp.install)
        _agent(page, webapp)
        chip = page.locator('[data-el="savedChip"]')
        expect(chip).to_contain_text("1 run", timeout=10_000)
        assert "1 runs" not in chip.inner_text()

    def test_the_label_holds_when_there_is_nothing_saved(self, webapp, page):
        """A team with no priced usage renders no total row. The window is a
        property of the read, not of having spent - it must not vanish with
        the figures it qualifies."""
        card = self._open(page, webapp)
        expect(card.locator(".eyebrow")).to_have_text("saved · lifetime")
