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
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect  # noqa: E402

from tests.test_webapp_runs import NOW, _monitor, _session, _workflow  # noqa: E402
from tests.metrics.helpers import seed_dashboard  # noqa: E402

RUNS_URL = re.compile(r"/api/agents/[^/]+/runs\?")
DETAILS_URL = re.compile(r"/api/agents/[^/]+/runs/[^/]+/details")
TRANSCRIPT_URL = re.compile(r"/api/agents/[^/]+/subagents/[^/]+/transcript")
CHAT_URL = re.compile(r"/api/agents/[^/]+/chat$")
CHAT_JOB_URL = re.compile(r"/api/agents/[^/]+/chat/[^/]+$")
RESUME_URL = re.compile(r"/resume")

# The runs table pages at 100. One row past it is the whole pager contract.
PAGE_SIZE = 100


class TestMetricsView:
    def test_turn_io_session_picker_and_unified_breakdown(self, webapp, page, monkeypatch):
        from bobi.metrics.store import connect

        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)
        conn = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='e2e-worker-a' WHERE session_id='s1'")
        conn.execute("UPDATE sessions SET provider_session_id='current-provider' WHERE session_id='s2'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        prompt = "Event: slack/message\n  Explain **WAL** and `SQLite`. <img src=x onerror=window.metricsInjected=true>\n  conversation: slack:T123:channel:C123:thread:1791360056.16\n  channel_name: eng-team"
        response = "## Answer\nA **write-ahead log**.\n```sql\nPRAGMA journal_mode=WAL;\n```"
        def stamp(offset):
            return datetime.fromtimestamp((started + offset) / 1_000_000, timezone.utc).isoformat()
        _seed_transcript(webapp, monkeypatch, "worker-a", [
            _entry("user", prompt, stamp(100_000)),
            _entry("assistant", response, stamp(800_000)),
            _entry("user", "Different turn", stamp(1_100_000)),
            _entry("assistant", "Different answer", stamp(1_800_000)),
        ])
        (webapp.install.sessions_dir / "worker-a.id").write_text("current-provider")
        page.goto(webapp.agent_url() + "/metrics")
        picker = page.get_by_role("combobox", name="Session lifecycle")
        expect(picker.locator("option")).to_have_count(3)
        expect(picker.locator('option[value="s1"]')).to_contain_text("worker-a (s1)")
        expect(picker.locator('option[value="s2"]')).to_contain_text("worker-a (s2)")
        picker.select_option("s1")
        expect(page.locator(".metrics-session-chip")).to_contain_text("Session: s1")
        rows = page.locator(".metrics-table-panel .runs tbody tr")
        expect(rows).to_have_count(3)
        routine = rows.filter(has_text="72%")
        expect(routine).to_contain_text("Slack #eng-team")
        expect(routine).to_contain_text("Explain **WAL**")
        expect(routine.locator("td").nth(1)).to_contain_text("Explain **WAL**")
        expect(routine.get_by_role("link", name="Slack #eng-team", exact=False)).to_have_attribute(
            "href", "https://app.slack.com/client/T123/C123/thread/C123-1791360056.16")
        routine.focus()
        routine.press("Enter")
        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail.locator(".metrics-io-block").first).to_contain_text("Explain WAL and SQLite.")
        expect(detail.locator(".metrics-markdown strong").first).to_have_text("WAL")
        expect(detail.locator(".metrics-markdown img")).to_have_count(0)
        assert page.evaluate("window.metricsInjected") is None
        expect(detail.locator(".metrics-io-block").last).to_contain_text("A write-ahead log.")
        expect(detail.locator(".metrics-io-block").last.locator("pre code")).to_contain_text("PRAGMA journal_mode=WAL;")
        expect(detail).not_to_contain_text("Different answer")
        detail.locator('[data-tab="technical"]').click()
        expect(detail.get_by_text("Model Invocations & Token Breakdown", exact=True)).to_have_count(1)
        expect(detail).not_to_contain_text("Token Usage & Cache Performance")
        expect(detail.locator(".panel-usage tbody tr")).to_have_count(1)
        expect(detail.locator(".panel-usage tbody tr")).to_contain_text("100")
        expect(detail.locator(".panel-usage tbody tr")).to_contain_text("not recorded")
        detail.locator('[data-tab="overview"]').click()
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        detail.get_by_role("button", name="Copy Input / Prompt", exact=True).click()
        expect(detail.get_by_role("button", name="Copy Input / Prompt", exact=True)).to_have_text("copied")
        assert page.evaluate("navigator.clipboard.readText()") == prompt
        detail.screenshot(path="/tmp/bobi-metrics-turn-io.png")
        detail.locator('[data-tab="technical"]').click()
        detail.locator(".panel-usage").scroll_into_view_if_needed()
        detail.screenshot(path="/tmp/bobi-metrics-unified-breakdown.png")
        detail.get_by_role("button", name="Close").click()
        picker.select_option("s2")
        expect(rows).to_have_count(1)
        expect(page.locator(".metrics-session-chip")).to_contain_text("Session: s2")
        page.screenshot(path="/tmp/bobi-metrics-lifecycle-picker.png", full_page=True)

    def test_metrics_navigation_filters_drilldown_and_refresh(self, webapp, page):
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _agent(page, webapp)
        page.get_by_role("link", name="metrics & routing", exact=True).click()
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        expect(page.locator(".metrics-tile")).to_have_count(3)
        expect(page.locator(".tile-input .tile-sub")).to_have_text("100 (13.9%) cache read (included)")
        expect(page.locator(".tile-input .tile-badge, .tile-output .tile-badge")).to_have_count(0)
        expect(page.locator(".metrics-page")).to_contain_text("2 / 1")
        rows = page.locator(".metrics-table-panel .runs tbody tr")
        expect(rows).to_have_count(4)
        expect(rows.filter(has_text="Router Decision")).to_have_count(2)
        expect(rows.filter(has_text="Sticky Session")).to_have_count(1)
        expect(rows.filter(has_text="Fallback")).to_have_count(1)
        expect(rows.filter(has_text="Fallback").locator("td").nth(3)).to_have_text("—")
        page.screenshot(path="/tmp/bobi-metrics-overview.png", full_page=True)
        routine = rows.filter(has_text="72%")
        expect(routine).to_contain_text("100 tok")
        routine.click()
        detail = page.get_by_role("dialog", name="Turn detail")
        detail.locator('[data-tab="technical"]').click()
        expect(detail).to_contain_text("ds/deepseek-flash")
        expect(detail).to_contain_text("deepseek-flash")
        detail.screenshot(path="/tmp/turn-detail-tables.png")
        page.locator(".detail-nav-tabs [data-tab=technical]").click()
        expect(page.locator(".panel-routing")).to_be_hidden()
        expect(page.locator(".panel-usage")).to_be_visible()
        detail.screenshot(path="/tmp/turn-detail-usage-tab.png")
        page.locator(".detail-nav-tabs [data-tab=overview]").click()
        expect(page.locator(".panel-routing")).to_be_visible()
        expect(page.locator(".panel-usage")).to_be_hidden()
        detail.screenshot(path="/tmp/turn-detail-routing-tab.png")
        page.locator(".detail-nav-tabs [data-tab=technical]").click()
        expect(page.locator(".panel-routing")).to_be_hidden()
        expect(page.locator(".panel-usage")).to_be_visible()
        page.screenshot(path="/tmp/bobi-metrics-dashboard.png", full_page=True)
        with page.expect_response(re.compile(r"/metrics/summary\?")):
            page.wait_for_timeout(10500)
        expect(detail).to_be_visible()
        detail.get_by_role("button", name="Close").click()
        page.get_by_role("textbox", name="Provider model filter").fill("deepseek-v4-pro")
        page.get_by_role("textbox", name="Provider model filter").press("Tab")
        expect(page.locator(".metrics-tile strong").first).to_have_text("620")
        expect(rows.filter(has_text="72%")).to_have_count(0)
        rows.filter(has_text="Sticky Session").click()
        expect(detail).to_contain_text("96%")
        expect(detail).not_to_contain_text("private-task")
        detail.get_by_role("button", name="Close").click()
        page.get_by_role("link", name=webapp.agent, exact=False).first.click()
        expect(page.locator(".metrics-page")).to_have_count(0)
        page.wait_for_timeout(500)
        assert errors == []

    def test_curator_sleep_cycle_turn_renders_cleanly_with_margins(self, webapp, page, monkeypatch):
        from bobi.metrics.store import connect

        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)
        conn = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE sessions SET session_id='ses_dabfcc7634bd07e444b889b44d0da7297c8611305855adf63eab16ad41eda7c5', provider_session_id='e2e-curator', session_name='curator-curator-05ff2ed8-curator' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET turn_id='turn_01a117cb-7aec-7d84-a959-e912efb493f6', session_id='ses_dabfcc7634bd07e444b889b44d0da7297c8611305855adf63eab16ad41eda7c5', turn_index=1, ended_at_us=started_at_us+900000 WHERE turn_id='t-routine'")
        conn.execute("UPDATE llm_invocations SET turn_id='turn_01a117cb-7aec-7d84-a959-e912efb493f6', model_selected='claude-haiku-4-5-20251001' WHERE invocation_id='i0'")
        conn.execute("INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,model_requested,model_selected,started_at_us,status) VALUES('i0-step2','turn_01a117cb-7aec-7d84-a959-e912efb493f6',1,'anthropic','claude-haiku','claude-haiku-4-5-20251001',?,'completed')", (started + 400000,))
        conn.execute("INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,model,measurement_source,is_estimated,input_tokens,output_tokens,cache_read_input_tokens,observed_at_us,token_semantics_version) VALUES('u-inv-2','invocation','turn_01a117cb-7aec-7d84-a959-e912efb493f6','i0-step2','anthropic','claude-haiku-4-5-20251001','provider_stream',0,79032,1,76193,?,1)", (started + 400000,))
        conn.execute("INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) VALUES('cost-turn','turn','ses_dabfcc7634bd07e444b889b44d0da7297c8611305855adf63eab16ad41eda7c5','turn_01a117cb-7aec-7d84-a959-e912efb493f6','anthropic','claude-haiku-4-5-20251001',0.261109,'provider_stream',0,?)", (started + 800000,))
        conn.commit()
        conn.close()

        def stamp(offset):
            return datetime.fromtimestamp((started + offset) / 1_000_000, timezone.utc).isoformat()

        sleep_prompt = "You are an agent in a bobi deployment. You are the **sleep cycle** for this agent team. You run out-of-band, on a schedule, as a monitor — to process recent transcript deltas, keep memory compact, and maintain durable state."
        sleep_response = "I'm the sleep cycle for this agent team. Let me process the transcript delta and update the durable memory."
        _seed_transcript(webapp, monkeypatch, "curator", [
            _entry("user", sleep_prompt, stamp(100_000)),
            _entry("assistant", sleep_response, stamp(800_000)),
        ])
        (webapp.install.sessions_dir / "curator.id").write_text("e2e-curator-session")

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(webapp.agent_url() + "/metrics")

        picker = page.get_by_role("combobox", name="Session lifecycle")
        picker.select_option("ses_dabfcc7634bd07e444b889b44d0da7297c8611305855adf63eab16ad41eda7c5")

        # Click the curator turn (t1, which has the seeded transcript)
        turn_row = page.locator(".metrics-table-panel .runs tbody tr").last
        expect(turn_row).to_be_visible()
        turn_row.click()

        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail).to_be_visible()

        # Check margins
        box = detail.bounding_box()
        assert box is not None
        assert box["width"] <= 1040
        backdrop_padding_x = (1440 - box["width"]) / 2
        backdrop_padding_y = (900 - box["height"]) / 2
        assert backdrop_padding_x >= 48
        assert backdrop_padding_y >= 36

        # Check content is rendered and not blank
        expect(detail.locator(".metrics-io-head").first).to_contain_text("Turn conversation transcript")
        expect(detail.locator(".io-block-user, .io-block-system")).to_contain_text("sleep cycle")
        expect(detail.locator(".io-block-assistant")).to_contain_text("sleep cycle for this agent team")

        # Save screenshot for visual inspection
        artifact_dir = "/Users/zodinet17/.gemini/antigravity-cli/brain/9cd6d59b-ad40-4739-84e0-f40111fa8838"
        page.screenshot(path=f"{artifact_dir}/verified_curator_modal_margins.png")
        detail.screenshot(path=f"{artifact_dir}/verified_curator_overview.png")

        # Switch to technical telemetry tab
        detail.locator('[data-tab="technical"]').click()
        expect(detail.locator(".panel-usage")).to_be_visible()
        detail.locator(".panel-usage th").last.scroll_into_view_if_needed()
        detail.screenshot(path=f"{artifact_dir}/verified_curator_technical.png")

        assert errors == []

    def test_filter_change_during_read_and_navigation_abort(self, webapp, page):
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        held = []

        def hold_first(route):
            if not held:
                held.append(route)
            else:
                route.continue_()

        page.route(re.compile(r"/metrics/summary\?"), hold_first)
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.get_by_role("textbox", name="Provider model filter")).to_be_visible()
        page.get_by_role("textbox", name="Provider model filter").fill("deepseek-v4-pro")
        page.get_by_role("textbox", name="Provider model filter").press("Tab")
        assert held
        held[0].continue_()
        expect(page.locator(".metrics-tile strong").first).to_have_text("620")
        held.clear()
        page.get_by_role("button", name="refresh", exact=True).click()
        expect(page.get_by_role("button", name="refresh", exact=True)).to_be_disabled()
        page.get_by_role("link", name=webapp.agent, exact=False).first.click()
        expect(page.locator(".metrics-page")).to_have_count(0)
        expect(page.locator("#health")).not_to_have_class("dot stale")

    def test_metrics_run_slab_preserves_transcript_and_composer(self, webapp, page, monkeypatch):
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        _seed_runs(webapp.install)
        _seed_transcript(webapp, monkeypatch, "worker-a", [
            _entry("assistant", "Original transcript stays available", "2026-08-01T09:15:00Z"),
        ])
        _agent(page, webapp)
        row = page.locator(".runs tbody tr", has_text="Fix the flaky test")
        row.click()
        expect(page.locator(".transcript")).to_contain_text("Original transcript stays available")
        expect(page.locator("[data-el=slabComposer]")).to_be_hidden()
        expect(page.locator("[data-el=slabTabs]")).to_be_hidden()
        page.locator("[data-el=slabClose]").click()
        expect(page.locator(".modal-backdrop")).not_to_have_class("modal-backdrop open")
        row.locator(".metrics-jump-link").click()
        page.wait_for_url(re.compile(r"/metrics\?session=worker-a$"))

    @pytest.mark.parametrize("width", [1440, 700])
    def test_shared_layout_and_compact_metrics_contract(self, webapp, page, width):
        page.set_viewport_size({"width": width, "height": 1000})
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        edges = []
        for url, selector in [(webapp.url, ".page"), (webapp.agent_url(), ".agent-content"),
                              (webapp.agent_url() + "/metrics", ".metrics-content")]:
            page.goto(url)
            expect(page.locator(selector)).to_be_visible()
            edges.append(page.locator(selector).evaluate("el => {const r=el.getBoundingClientRect(); const s=getComputedStyle(el); return [r.x, r.width, s.paddingLeft, s.paddingRight]}"))
        assert edges[0] == edges[1] == edges[2]
        assert edges[0][1] == min(width, 1360)
        assert edges[0][2:] == ["18px", "18px"] if width <= 760 else edges[0][2:] == ["32px", "32px"]
        expect(page.locator(".metrics-table-panel thead th")).to_have_text(
            ["Turn", "Session / Topic", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"])
        expect(page.locator(".metrics-active-filter-bar")).to_have_count(0)
        rows = page.locator(".metrics-table-panel tbody tr")
        expect(rows).to_have_count(4)
        for cell in rows.locator("td:nth-child(4)").all():
            assert re.fullmatch(r"(?:[0-9]+%|—)", cell.inner_text())
        rows.filter(has_text="72%").click()
        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail.locator(".detail-nav-tabs button")).to_have_text(["Overview & Execution", "Technical Telemetry"])
        expect(detail.locator(".panel-routing thead th")).to_have_text(["Model Used", "Routing Decision & Reason", "Confidence", "Latency"])
        expect(detail.locator(".metrics-turn-empty-io")).to_have_count(1)
        expect(detail.locator(".metrics-io-block")).to_have_count(0)
        expect(detail.locator(".panel-tools")).to_have_count(0)
        expect(detail.locator("textarea")).to_have_count(0)
        expect(detail).not_to_contain_text("origin not recorded")
        expect(detail).not_to_contain_text("Session history is not substituted")
        contrast = detail.locator(".metrics-turn-empty-io, .panel-routing th, .decision-reason-text, .metrics-kpi-strip .metrics-pair > span").evaluate_all("""elements => {
            const context = document.createElement('canvas').getContext('2d');
            const rgba = color => {
                context.clearRect(0, 0, 1, 1);
                context.fillStyle = color;
                context.fillRect(0, 0, 1, 1);
                return [...context.getImageData(0, 0, 1, 1).data].map(value => value / 255);
            };
            const blend = (front, back) => front.slice(0, 3).map((value, index) => value * front[3] + back[index] * (1 - front[3]));
            const luminance = color => color.map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((total, value, index) => total + value * [.2126, .7152, .0722][index], 0);
            return elements.map(element => {
                const chain = [];
                for (let parent = element; parent; parent = parent.parentElement) chain.unshift(parent);
                let background = [1, 1, 1];
                for (const parent of chain) background = blend(rgba(getComputedStyle(parent).backgroundColor), background);
                const foreground = blend(rgba(getComputedStyle(element).color), background);
                const levels = [luminance(background), luminance(foreground)].sort((first, second) => first - second);
                return {text: element.textContent, ratio: (levels[1] + .05) / (levels[0] + .05)};
            });
        }""")
        assert all(item["ratio"] >= 4.5 for item in contrast), [item for item in contrast if item["ratio"] < 4.5]
        detail.screenshot(path=f"/tmp/bobi-overhaul-overview-{width}.png")
        detail.locator('[data-tab="technical"]').click()
        detail.screenshot(path=f"/tmp/bobi-overhaul-technical-{width}.png")
        detail.get_by_role("button", name="Close", exact=True).click()
        page.get_by_role("combobox", name="Session lifecycle").select_option("s1")
        expect(page.locator(".metrics-session-chip")).to_have_count(1)
        expect(rows).to_have_count(3)
        page.get_by_role("button", name="Clear session filter").click()
        expect(page.locator(".metrics-session-chip")).to_be_hidden()
        expect(rows).to_have_count(4)
        page.get_by_role("searchbox", name="Search loaded turns").fill("no-match")
        expect(rows).to_have_count(0)
        page.get_by_role("checkbox", name="Auto-refresh").uncheck()
        with page.expect_response(re.compile(r"/metrics/summary\?")):
            page.get_by_role("checkbox", name="Auto-refresh").check()
        page.get_by_role("combobox", name="Time range").select_option("744")
        expect(page.get_by_role("combobox", name="Time range")).to_have_value("744")
        page.screenshot(path=f"/tmp/bobi-overhaul-layout-{width}.png", full_page=True)

    @pytest.mark.parametrize("mode,fallback,expected", [
        ("enforce", "policy_low_confidence", "Fallback"),
        ("enforce", "policy_circuit_open", "Fallback (Breaker)"),
        ("shadow", None, "Shadow Evaluation"),
    ])
    def test_routing_explanation_uses_only_recorded_facts(self, webapp, page, mode, fallback, expected):
        from bobi.metrics.store import connect

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        conn = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        conn.execute("UPDATE router_decisions SET fallback_reason=?,metadata_json=json_set(metadata_json,'$.policy.mode',?,'$.policy.confidence',0.34,'$.policy.recommended_model','ds/deepseek-v4-pro') WHERE turn_id='t-routine'", (fallback, mode))
        conn.commit()
        conn.close()
        page.goto(webapp.agent_url() + "/metrics")
        row = page.locator(".metrics-table-panel tbody tr", has_text="34%")
        expect(row).to_contain_text("deepseek-flash")
        expect(row).to_contain_text(expected)
        row.click()
        detail = page.get_by_role("dialog", name="Turn detail")
        decision = detail.locator(".panel-routing tbody tr")
        expect(decision.locator("td").nth(0)).to_contain_text("deepseek-flash")
        expect(decision.locator("td").nth(1)).to_contain_text(expected)
        expect(decision.locator("td").nth(2)).to_have_text("34%")
        expect(decision.locator("td").nth(2).locator("span")).to_have_attribute("title", "Raw score: 0.340")
        expect(decision).not_to_contain_text("threshold")
        expect(decision).not_to_contain_text("rate limit")
        expect(decision).not_to_contain_text("anthropic")
        if mode == "shadow":
            expect(decision).to_contain_text("Recommended ds/deepseek-v4-pro; kept the configured model ds/deepseek-flash")
        else:
            expect(decision).to_contain_text("executed ds/deepseek-flash")

    def test_tool_cards_are_compact_and_escape_transcript_text(self, webapp, page, monkeypatch):
        from bobi.metrics.store import connect

        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)
        conn = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='e2e-worker-a' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.execute("INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,tool_kind,started_at_us,ended_at_us,status) VALUES('tool-ui','t-routine','i0','Bash','shell',?,?,'completed')", (started + 200000, started + 400000))
        conn.commit()
        conn.close()
        at = datetime.fromtimestamp((started + 200000) / 1_000_000, timezone.utc).isoformat()
        _seed_transcript(webapp, monkeypatch, "worker-a", [
            json.dumps({"type": "assistant", "timestamp": at, "message": {"content": [{"type": "tool_use", "id": "ui-tool", "name": "Bash", "input": {"command": '<img src=x onerror="window.metricsInjected=true">'}}]}}),
        ])
        page.goto(webapp.agent_url() + "/metrics")
        row = page.locator(".metrics-table-panel tbody tr", has_text="72%")
        expect(row).to_contain_text("1 tool calls")
        row.click()
        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail.locator(".metrics-tool-card")).to_have_count(1)
        assert not detail.locator(".metrics-tool-card").evaluate("el => el.open")
        detail.locator(".metrics-tool-card summary").click()
        expect(detail.locator(".metrics-tool-body pre")).to_contain_text("metricsInjected")
        expect(detail.locator("img")).to_have_count(0)
        assert page.evaluate("window.metricsInjected") is None

    def test_metrics_unavailable_empty_and_untrusted_model_text(self, webapp, page):
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.get_by_role("status")).to_contain_text("metrics database is not ready")
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        page.get_by_role("button", name="refresh", exact=True).click()
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        from bobi.metrics.store import connect

        connection = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        payload = '<img src=x onerror="window.metricsInjected=true">'
        connection.execute("UPDATE router_decisions SET model_selected=?", (payload,))
        connection.commit()
        connection.close()
        page.get_by_role("button", name="refresh", exact=True).click()
        expect(page.locator(".metrics-page")).to_contain_text(payload)
        assert page.locator(".metrics-page img").count() == 0
        assert page.evaluate("window.metricsInjected") is None
        page.goto(webapp.agent_url() + "/metrics?session=absent")
        expect(page.locator(".metrics-page")).to_contain_text("No recorded turns in this window.")
        expect(page.locator(".metrics-tile strong").first).to_have_text("not recorded")


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

    def test_a_hostile_agent_name_renders_as_text_not_markup(self, webapp,
                                                             page):
        # The route's name is client-derived (`decodeURIComponent` on the
        # hash), so the view can be handed ANY string regardless of what the
        # server would accept at install time. This drives a live payload
        # through the two sinks such a name deterministically reaches: the
        # missing-agent stub (nothing by that name is installed) and the top
        # bar's subtitle. Agent OUTPUT has its own hostile test in
        # TestRunModal.
        hostile = '<img src=x onerror="window.__pwned=1">&"rogue"'
        _agent(page, webapp, hostile)
        expect(page.locator(".agent-page .stub h2")).to_have_text(hostile)
        expect(page.locator("#subtitle")).to_have_text(hostile)
        assert page.evaluate("window.__pwned") is None
        assert page.locator(".agent-page img").count() == 0

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
            ["all", "manager · 0", "monitor · 1", "running · 0", "awaiting action · 1", "failed · 1"])
        expect(page.locator("[data-el=runsCount]")).to_have_text("4")

        page.locator(".tabs .tab", has_text="failed").click()
        expect(page.locator(".runs tbody tr")).to_have_count(1)
        expect(page.locator(".runs tbody tr .r-title")).to_have_text(
            "Ship the migration")
        # Counts describe the whole set, not the filtered page.
        expect(tabs).to_have_text(
            ["all", "manager · 0", "monitor · 1", "running · 0", "awaiting action · 1", "failed · 1"])

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
        expect(lines.nth(0).locator(".ts")).to_have_text(re.compile(r"(?:09|16):15:00"))
        expect(lines.nth(0).locator(".who")).to_have_text("user")
        expect(lines.nth(0).locator(".txt")).to_have_text("fix the flaky test")
        expect(lines.nth(1).locator(".who")).to_have_text("agent")
        expect(lines.nth(1).locator(".txt")).to_have_text("found the race")
        # A tool call is its own line: the thing the chat view throws away.
        expect(lines.nth(2)).to_have_class("tr-line tool")
        expect(lines.nth(2).locator(".txt")).to_have_text(
            "Bash: pytest -q tests/test_flaky.py")

    def test_hostile_agent_output_renders_as_text_not_markup(self, webapp,
                                                             page,
                                                             monkeypatch):
        # Agent OUTPUT is the prompt-injectable surface: a reply — and the
        # run title that echoes it — can carry any markup an injected prompt
        # asks for. The runs table, the slab title, and the transcript lines
        # all render through `mk()`/`textContent`; this proves that against
        # the DOM with a live payload. It replaces the deleted renderer
        # suite's job for the output surfaces that still exist.
        payload = '<img src=x onerror="window.__pwned=1">rogue output'
        _session(webapp.install, "worker-x", status="completed",
                 title=payload)
        _seed_transcript(webapp, monkeypatch, "worker-x", [
            _entry("assistant", [{"type": "text", "text": payload}],
                   "2026-08-01T09:15:00.000Z"),
        ])
        _agent(page, webapp)

        row = page.locator(".runs tbody tr", has_text="rogue output")
        expect(row).to_have_count(1)
        row.click()
        expect(page.locator("[data-el=slabKind]")).to_have_text("transcript")
        expect(page.locator("[data-el=slabTitle]")).to_have_text(payload)
        expect(page.locator(".transcript .tr-line .txt")).to_have_text(payload)
        assert page.evaluate("window.__pwned") is None
        assert page.locator(".runs img, .modal-backdrop img").count() == 0

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
    """Inspection is read-only; only a paused approval gate submits a verdict."""

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

    def test_live_session_inspection_is_read_only(self, webapp, page):
        self._seed(webapp.install)
        posted = self._chat(page)
        _agent(page, webapp)
        page.locator(".runs tbody tr", has_text="Live worker").first.click()
        expect(page.locator(".composer")).to_be_hidden()
        expect(page.locator(".modal textarea")).to_have_count(0)
        assert posted == []

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
        expect(composer).to_be_hidden()
        expect(composer.locator("textarea")).to_have_count(0)
        expect(composer.locator("button")).to_have_count(0)

    def test_closing_the_slab_takes_the_composer_with_it(self, webapp, page):
        self._seed(webapp.install)
        _agent(page, webapp)
        page.locator(".runs tbody tr", has_text="Live worker").first.click()
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
