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

One thing is driven from the browser side instead, and only one, because it
needs something this process cannot have offline: a server that fails a read on
demand (the `runsError` branch). It has an unstubbed companion test alongside
it.

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
TRANSCRIPT_URL = re.compile(r"/api/agents/[^/]+/subagents/[^/]+/transcript")
CHAT_URL = re.compile(r"/api/agents/[^/]+/chat$")
CHAT_JOB_URL = re.compile(r"/api/agents/[^/]+/chat/[^/]+$")
RESUME_URL = re.compile(r"/resume")

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

    def test_an_awaiting_row_offers_close_and_no_reminder(self, webapp, page):
        # Remind was cut from this page (MOD-371): the button never delivered.
        # The endpoint behind it stays, for the CLI and the hosted admin
        # protocol, so the guard belongs here, on what the row renders.
        _seed_runs(webapp.install)
        _agent(page, webapp)

        actions = self._gate_row(page).locator(".row-actions button")
        expect(actions).to_have_text(["Transcript", "Details", "Close"])

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


# --- the composer -----------------------------------------------------------

class TestComposer:
    """The reply box at the foot of a transcript (#987).

    Three branches, and the read model picks between them: a live session is
    delivered to, a run parked on a human gate is ANSWERED, and a row that is
    neither gets no control at all. What each branch posts is the assertion
    here - the payload is the entire contract, and a box that sends the wrong
    thing looks exactly like a box that works.

    The chat and resume endpoints are answered from the browser side, for the
    same reason the `runsError` branch is: resolving one for real needs a live
    agent process to take a turn, which this test process does not have.
    Delivery is proven against real sessions in
    `tests/integration/test_webapp_chat_delivery.py`, and what a verdict does
    to a running workflow in `tests/test_orchestrator.py`; what is proven here
    is the front end, which neither of those can see.
    """

    LIVE = "worker-live"
    GATE = "wf-issue-lifecycle-test-repo-987"

    def _seed(self, install):
        """One live session and one parked gate, both with a transcript."""
        _session(install, self.LIVE, status="running", title="Live worker",
                 terminal_at=0.0)
        _session(install, self.GATE, status="waiting", title="gate session",
                 terminal_at=0.0)
        _workflow(install, "11d31ce5", status="waiting", name="issue-lifecycle",
                  started_at=NOW - 90000, completed_at=0, suspended_at_step=7,
                  await_event="approval", session_name=self.GATE,
                  run_key="987")

    def _chat(self, page, *, outcome="done", error=""):
        """Answer the chat endpoints and hand back the POSTed payloads."""
        posted = []

        def submit(route, request):
            posted.append(json.loads(request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"message_id": "mid-1"}))

        job = {"status": outcome}
        if error:
            job["error"] = error
        page.route(CHAT_URL, submit)
        page.route(CHAT_JOB_URL, lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(job)))
        return posted

    def _resume(self, page, *, accepted=True, error=""):
        """Answer the resume route and hand back the POSTed bodies."""
        posted = []

        def submit(route, request):
            posted.append(json.loads(request.post_data))
            body = {"ok": True, "accepted": True, "run_id": "11d31ce5",
                    "workflow": "issue-lifecycle", "await_event": "approval",
                    "verdict": posted[-1].get("verdict", "")}
            if not accepted:
                body = {"error": error or "run wf-1 is 'completed'"}
            route.fulfill(status=200 if accepted else 409,
                          content_type="application/json",
                          body=json.dumps(body))

        page.route(RESUME_URL, submit)
        return posted

    def _open(self, page, webapp, title):
        _agent(page, webapp)
        page.locator(".runs tbody tr", has_text=title).first.click()
        expect(page.locator(".composer")).to_be_visible()
        return page.locator(".composer")

    def test_a_live_session_offers_a_reply(self, webapp, page):
        self._seed(webapp.install)
        composer = self._open(page, webapp, "Live worker")
        expect(composer.locator("button")).to_have_text("Send")
        expect(composer.locator(".composer-note")).to_contain_text(
            "the same way the CLI delivers a message")

    def test_a_parked_gate_offers_a_verdict_instead(self, webapp, page):
        # Nothing is behind this row, and nothing needs to be: what the gate
        # is waiting for is an answer, not a message.
        self._seed(webapp.install)
        composer = self._open(page, webapp, "issue-lifecycle")
        expect(composer.locator("button")).to_have_text(["Reject", "Approve"])
        expect(composer.locator(".composer-note")).to_contain_text(
            "This run is awaiting approval")
        # The gate is state, and state renders violet (design-system rule 2).
        expect(composer).to_have_class(re.compile(r"\bgate\b"))

    def test_a_row_with_no_session_gets_no_composer(self, webapp, page):
        # The details branch. There is no session, so there is nothing to
        # talk to and nothing to continue.
        _seed_runs(webapp.install)
        _agent(page, webapp)
        page.locator(".runs tbody tr", has_text="inbox-watch").click()
        expect(page.locator("[data-el=slabKind]")).to_have_text("details")
        expect(page.locator(".composer")).to_be_hidden()

    def test_the_live_branch_delivers_to_that_session(self, webapp, page):
        self._seed(webapp.install)
        posted = self._chat(page)
        composer = self._open(page, webapp, "Live worker")

        composer.locator("textarea").fill("how is it going?")
        composer.locator("button").click()

        expect(composer.locator("textarea")).to_have_value("", timeout=10_000)
        assert posted == [{"subagent": self.LIVE, "text": "how is it going?"}]

    def test_the_live_branch_re_reads_the_transcript_when_the_turn_lands(
            self, webapp, page):
        """The slab is otherwise one-shot: the 4s timers refresh the table,
        not this. Without the re-read the answer is invisible until the run
        is reopened, which reads as the message having been swallowed."""
        self._seed(webapp.install)
        self._chat(page)
        composer = self._open(page, webapp, "Live worker")

        reread = _record_requests(page, TRANSCRIPT_URL)
        composer.locator("textarea").fill("ping")
        composer.locator("button").click()
        expect(composer.locator("textarea")).to_have_value("", timeout=10_000)
        assert len(reread) == 1, reread

    def test_approving_resumes_the_run_with_that_verdict(self, webapp, page):
        """The verdict is the payload. The route step in the workflow reads it
        back as `${{event.verdict}}`, so a button that posts the wrong word
        sends the run down the wrong branch."""
        self._seed(webapp.install)
        posted = self._resume(page)
        composer = self._open(page, webapp, "issue-lifecycle")

        composer.locator("textarea").fill("looks right")
        composer.get_by_role("button", name="Approve").click()

        expect(composer.locator(".composer-status")).to_contain_text(
            "Approved", timeout=10_000)
        assert posted == [{"verdict": "approve", "reply": "looks right"}]

    def test_rejecting_posts_the_other_verdict_and_the_reason(self, webapp,
                                                              page):
        """Same route, different answer. The typed text is the reason, not the
        decision: it rides along so the rework step can see why."""
        self._seed(webapp.install)
        posted = self._resume(page)
        composer = self._open(page, webapp, "issue-lifecycle")

        composer.locator("textarea").fill("scope is too wide")
        composer.get_by_role("button", name="Reject").click()

        expect(composer.locator(".composer-status")).to_contain_text(
            "Rejected", timeout=10_000)
        assert posted == [{"verdict": "reject", "reply": "scope is too wide"}]

    def test_a_verdict_needs_no_reason(self, webapp, page):
        """An approval with nothing to add is the common case, and a control
        that refuses to fire on an empty box would read as broken."""
        self._seed(webapp.install)
        posted = self._resume(page)
        composer = self._open(page, webapp, "issue-lifecycle")

        composer.get_by_role("button", name="Approve").click()

        expect(composer.locator(".composer-status")).to_contain_text(
            "Approved", timeout=10_000)
        assert posted == [{"verdict": "approve", "reply": ""}]

    def test_enter_does_not_answer_the_gate(self, webapp, page):
        """The live branch sends on Enter, because there is one thing Enter
        could mean. Here there are two verdicts and no default - a spec
        approved by a stray keystroke is exactly the failure this design is
        replacing."""
        self._seed(webapp.install)
        posted = self._resume(page)
        composer = self._open(page, webapp, "issue-lifecycle")

        composer.locator("textarea").fill("hmm")
        composer.locator("textarea").press("Enter")
        page.wait_for_timeout(300)
        assert posted == []

    def test_a_refused_verdict_is_reported_inline_and_the_box_recovers(
            self, webapp, page):
        """A run the table still shows as waiting can already have moved. The
        refusal has to be named in the modal being read, not swallowed."""
        self._seed(webapp.install)
        self._resume(page, accepted=False,
                     error="run 11d31ce5 is 'completed', not 'waiting'")
        composer = self._open(page, webapp, "issue-lifecycle")

        composer.get_by_role("button", name="Approve").click()

        status = composer.locator(".composer-status")
        expect(status).to_have_text(
            "run 11d31ce5 is 'completed', not 'waiting'", timeout=10_000)
        expect(status).to_have_class("composer-status bad")
        expect(composer.get_by_role("button", name="Approve")).to_be_enabled()
        expect(composer.get_by_role("button", name="Reject")).to_be_enabled()

    def test_an_ended_session_with_no_gate_offers_nothing_to_send(
            self, webapp, page):
        """What replaced the manager relay. Nothing is behind this row and
        there is no verdict to give, so there is no control - a box that
        accepted typing here would be promising a delivery that cannot
        happen."""
        _seed_runs(webapp.install)
        _agent(page, webapp)
        page.locator(".runs tbody tr", has_text="Fix the flaky test").click()
        composer = page.locator(".composer")
        expect(composer).to_be_visible()
        expect(composer).to_contain_text("This session has ended")
        expect(composer.locator("textarea")).to_have_count(0)
        expect(composer.locator("button")).to_have_count(0)

    def test_a_failed_delivery_is_reported_inline_and_the_box_recovers(
            self, webapp, page):
        """The failure this whole design exists to avoid is a box that
        accepts typing and reports nothing. It must name the reason, in the
        modal being read, and hand the control back."""
        self._seed(webapp.install)
        self._chat(page, outcome="error",
                   error="session 'worker-live' process is dead")
        composer = self._open(page, webapp, "Live worker")

        composer.locator("textarea").fill("anyone there?")
        composer.locator("button").click()

        expect(composer.locator(".composer-status")).to_have_text(
            "session 'worker-live' process is dead", timeout=10_000)
        expect(composer.locator(".composer-status")).to_have_class(
            "composer-status bad")
        expect(composer.locator("button")).to_be_enabled()
        expect(composer.locator("textarea")).to_be_enabled()
        # Their words are not thrown away by a failure they did not cause.
        expect(composer.locator("textarea")).to_have_value("anyone there?")

    def test_the_control_is_disabled_while_the_turn_is_in_flight(self, webapp,
                                                                 page):
        # A turn can take minutes. Leaving the box live invites a second send
        # against a session already taking a turn.
        self._seed(webapp.install)
        self._chat(page, outcome="pending")
        composer = self._open(page, webapp, "Live worker")

        composer.locator("textarea").fill("slow one")
        composer.locator("button").click()
        expect(composer.locator("button")).to_be_disabled()
        expect(composer.locator("button")).to_have_text("Sending…")
        expect(composer.locator("textarea")).to_be_disabled()

    def test_closing_the_slab_takes_the_composer_with_it(self, webapp, page):
        self._seed(webapp.install)
        self._open(page, webapp, "Live worker")
        page.locator("[data-el=slabClose]").click()
        expect(page.locator(".composer")).to_be_hidden()


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
