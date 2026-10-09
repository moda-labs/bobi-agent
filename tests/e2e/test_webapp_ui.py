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
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

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
METRICS_CAPTURES = Path(__file__).resolve().parents[2] / ".tmp/metrics-modernization/hardening/screenshots"


def _metrics_capture(page, name):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
    METRICS_CAPTURES.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(METRICS_CAPTURES / f"{name}.png"), full_page=True, animations="disabled")


def _metrics_bounds(page, selector):
    failures = page.locator(selector).evaluate_all("""elements => elements
        .filter(element => element.getClientRects().length)
        .map(element => {
            const bounds = element.getBoundingClientRect();
            return {tag: element.tagName, id: element.id, width: bounds.width,
                left: bounds.left, right: bounds.right, viewport: innerWidth};
        }).filter(bounds => bounds.width < 24 || bounds.left < 0 || bounds.right > bounds.viewport + 1)""")
    assert not failures, failures
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


class TestMetricsView:
    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_metrics_model_input_filters_without_enter_while_loading(self, webapp, page, width):
        from bobi.metrics.store import connect

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        connection = connect(webapp.install.state_dir / "metrics/metrics.db")
        connection.execute("UPDATE llm_invocations SET model_requested='claude-haiku-4-5', model_selected='claude-haiku-4-5' WHERE invocation_id='i0'")
        connection.execute("UPDATE usage_measurements SET model='claude-haiku-4-5' WHERE measurement_id='u0'")
        connection.commit()
        connection.close()
        held = []
        requests = _record_requests(page, re.compile(r"/metrics/(?:summary|turns)\?"))
        def hold_initial_summary(route):
            if "model=" not in route.request.url:
                held.append(route)
            else:
                route.continue_()
        page.route("**/metrics/summary?**", hold_initial_summary)
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        model = page.get_by_role("textbox", name="Provider model filter")
        model.fill("haik")
        expect(model).to_be_focused()
        expect(page.locator("#metrics-refresh")).to_have_attribute("aria-busy", "true")
        page.wait_for_timeout(400)
        assert len(held) == 1
        held.pop().continue_()
        rows = page.locator(".metrics-table-panel tbody tr")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("claude-haiku-4-5")
        expect(page.locator(".metrics-tile strong").first).to_have_text("100")
        assert any("/summary?" in url and "model=haik" in url for url in requests)
        assert any("/turns?" in url and "model=haik" in url for url in requests)
        expect(model).to_be_focused()
        _metrics_capture(page, f"model-haik-{width}")
        model.fill("  V4-PRO  ")
        expect(rows).to_have_count(2)
        expect(page.locator(".metrics-tile strong").first).to_have_text("620")
        page.unroute("**/metrics/summary?**", hold_initial_summary)
        model.fill("")
        expect(rows).to_have_count(4)
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    @pytest.mark.parametrize("failure", ["404", "500", "hung"])
    def test_jev_header_falls_back_within_deadline(self, webapp, page, width, failure):
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        held = []
        def fail_config(route):
            if failure == "hung":
                held.append(route)
            else:
                route.fulfill(status=int(failure), json={"error": "offline_config_unavailable"})
        page.route("**/metrics/jev-config", fail_config)
        page.clock.install()
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        if failure == "hung":
            assert held
            page.clock.run_for(2500)
        button = page.get_by_role("button", name="JEV: Disabled")
        expect(button).to_be_visible(timeout=2500 if failure != "hung" else 250)
        expect(button).to_be_enabled()
        assert "Loading" not in button.inner_text()
        _metrics_capture(page, f"jev-{failure}-disabled-{width}")
        button.click()
        dialog = page.get_by_role("dialog", name="JEV Routing Configuration")
        if failure == "hung":
            page.clock.run_for(2500)
        expect(dialog).to_contain_text(re.compile(r"unavailable|could not|failed|timed out", re.I))
        expect(dialog.locator("#jev-config-save")).to_have_count(0)
        expect(dialog.locator("#jev-config-export, #jev-config-delete")).to_have_count(0)
        dialog.get_by_role("button", name="Close dialog").click()
        expect(dialog).to_be_hidden()
        assert errors == []
        for route in held:
            route.abort()

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    @pytest.mark.parametrize("mode", ["enforce", "shadow"])
    def test_jev_header_preserves_active_mode(self, webapp, page, width, mode):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        config = concise_config()
        config["mode"] = mode
        paths.env_path(webapp.install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + shlex.quote(json.dumps(config)) + "\nBOBI_METRICS_ASSIGNMENT_SECRET=browser-only-assignment-seed\n")
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        button = page.get_by_role("button", name="JEV: " + mode.title())
        expect(button).to_be_visible()
        expect(button).to_have_class(re.compile(r"\bactive\b"))
        expect(button).to_have_class(re.compile("mode-" + mode))
        expect(page.get_by_role("button", name="JEV: Disabled")).to_have_count(0)
        _metrics_capture(page, f"jev-{mode}-{width}")
        button.click()
        dialog = page.get_by_role("dialog", name="JEV Routing Configuration")
        dialog.get_by_role("tab", name="Configure").click()
        expect(dialog.locator("#jev-config-mode")).to_have_value(mode)

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_metrics_maintenance_collapses_without_reclassifying_real_user(self, webapp, page, monkeypatch, width):
        from bobi.metrics.store import connect

        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)
        connection = connect(webapp.install.state_dir / "metrics/metrics.db")
        connection.execute("UPDATE sessions SET provider_session_id='e2e-worker-a' WHERE session_id='s1'")
        connection.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        connection.execute("UPDATE turns SET trigger_kind='sleep_cycle', is_user_initiated=0 WHERE turn_id='t-routine'")
        connection.execute("UPDATE turns SET trigger_kind='startup', is_user_initiated=0 WHERE turn_id='t-pro'")
        connection.commit()
        connection.close()
        long_context = ("維護 context " * 500).rstrip()
        prompts = [
            "You are the **sleep cycle** for this agent team. You run out-of-band, on a schedule, as a monitor to process recent transcript deltas.\n" + long_context,
            "You are an agent in a bobi deployment. Initialize the agent.\n" + long_context,
            "Explain curator sleep cycle and memory compaction without reclassifying my message. " + "user-content-" * 600,
        ]
        entries = []
        for index, prompt in enumerate(prompts):
            at = datetime.fromtimestamp((started + index * 1_000_000 + 100_000) / 1_000_000, timezone.utc).isoformat()
            entries.extend([_entry("user", prompt, at), _entry("assistant", "Completed safely", at)])
        _seed_transcript(webapp, monkeypatch, "worker-a", entries)
        page.set_viewport_size({"width": width, "height": 1000})
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.goto(webapp.agent_url() + "/metrics")
        rows = page.locator(".metrics-table-panel tbody tr")
        expect(rows).to_have_count(4)
        for index, title in [(0, "Sleep Cycle & Memory Compaction"), (1, "Agent Startup & Initialization")]:
            rows.filter(has_text=title).click()
            dialog = page.get_by_role("dialog", name="Turn detail")
            expect(dialog.get_by_role("heading", name="User Message", exact=True)).to_have_count(0)
            heading = "System / Maintenance Prompt" if index == 0 else "Agent Startup & System Context"
            expect(dialog.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            content = dialog.locator(".io-block-system .metrics-prompt-content")
            expand = dialog.locator(".io-block-system .metrics-tool-expand")
            expect(expand).to_be_visible()
            expect(expand).to_have_attribute("aria-expanded", "false")
            assert content.text_content() == prompts[index]
            assert content.evaluate("element => element.clientHeight <= 180 && element.scrollHeight > element.clientHeight")
            _metrics_capture(page, f"prompt-{'maintenance' if index == 0 else 'startup'}-collapsed-{width}")
            expand.focus()
            expand.press("Enter")
            expect(expand).to_have_attribute("aria-expanded", "true")
            assert content.evaluate("element => element.clientHeight > 180 && element.scrollHeight <= element.clientHeight + 1")
            _metrics_capture(page, f"prompt-{'maintenance' if index == 0 else 'startup'}-expanded-{width}")
            expand.click()
            expect(expand).to_have_attribute("aria-expanded", "false")
            assert content.evaluate("element => element.clientHeight <= 180")
            dialog.get_by_role("button", name="Close", exact=True).click()
        user_row = rows.filter(has_text="Explain curator sleep cycle")
        expect(user_row.locator(".metrics-topic-title")).to_have_text("initial request")
        expect(user_row.locator(".metrics-topic-meta")).to_contain_text("Explain curator sleep cycle")
        user_row.click()
        dialog = page.get_by_role("dialog", name="Turn detail")
        expect(dialog.get_by_role("heading", name="User Message", exact=True)).to_be_visible()
        expect(dialog.get_by_role("heading", name="System / Maintenance Prompt", exact=True)).to_have_count(0)
        expect(dialog.locator(".metrics-full-prompt-details")).to_have_count(0)
        user = dialog.locator(".io-block-user .metrics-markdown")
        expect(user).to_contain_text(prompts[2])
        assert user.evaluate("element => element.clientHeight <= 180 && element.scrollHeight > element.clientHeight")
        _metrics_capture(page, f"prompt-real-user-collapsed-{width}")
        expand = dialog.locator(".io-block-user .metrics-tool-expand")
        expect(expand).to_have_attribute("aria-expanded", "false")
        expand.click()
        expect(expand).to_have_attribute("aria-expanded", "true")
        assert user.evaluate("element => element.clientHeight > 180 && element.scrollHeight <= element.clientHeight + 1")
        _metrics_capture(page, f"prompt-real-user-expanded-{width}")
        expand.click()
        expect(expand).to_have_attribute("aria-expanded", "false")
        dialog.get_by_role("button", name="Copy Input / Prompt", exact=True).click()
        expect(dialog.get_by_role("button", name="Copy Input / Prompt", exact=True)).to_have_text("copied")
        assert page.evaluate("navigator.clipboard.readText()") == prompts[2]

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_jev_sandbox_and_defensive_toolbar_layout(self, webapp, page, monkeypatch, width):
        import httpx
        from bobi import paths
        from bobi.metrics.store import connect
        from tests.metrics.test_routing import concise_config

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        database = webapp.install.state_dir / "metrics/metrics.db"
        connection = connect(database)
        connection.execute("UPDATE sessions SET session_name=?", ("session-name-" * 30,))
        connection.commit()
        before = "\n".join(connection.iterdump())
        connection.close()
        env_file = paths.env_path(webapp.install.repo_path)
        config = concise_config()
        config["endpoint"] = "https://api.typesafe.ai/v1/sandbox-test"
        config["prompt_egress"] = "redacted"
        env_file.write_text("# BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) + "\n")
        env_before = env_file.read_bytes()
        client_class = httpx.AsyncClient
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer browser-test-key"
            assert calls[-1]["state"]["role"] == "curator"
            assert calls[-1]["state"]["entry_point"] == "subagent_phase"
            if calls[-1]["state"]["task"] == "Fail deliberately":
                return httpx.Response(401, text="private vendor error")
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-v4-pro", "confidence": 0.94,
                "probabilities": {"ds/deepseek-flash": 0.03, "ds/deepseek-v4-pro": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "debug": '<img src=x onerror="window.sandboxInjected=true">'})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        toolbar = page.locator(".metrics-toolbar-card")
        layout = toolbar.evaluate("""element => {
            const outer = element.getBoundingClientRect();
            const controls = [...element.children].map(child => {
                const bounds = child.getBoundingClientRect();
                return {left: bounds.left, right: bounds.right, top: Math.round(bounds.top), height: bounds.height};
            });
            return {left: outer.left, right: outer.right, controls,
                rows: new Set(controls.map(control => control.top)).size,
                overflow: document.documentElement.scrollWidth > innerWidth};
        }""")
        assert layout["rows"] == (1 if width == 1440 else 2), layout
        assert not layout["overflow"], layout
        assert all(control["height"] >= 36 and control["left"] >= layout["left"]
                   and control["right"] <= layout["right"] for control in layout["controls"]), layout
        _metrics_bounds(page, ".metrics-toolbar-card input:not([role=switch]), .metrics-toolbar-card select, .metrics-toolbar-card button, .metrics-switch-track")
        _metrics_capture(page, f"toolbar-default-{width}")
        search = page.get_by_role("searchbox", name="Search loaded turns")
        search.focus()
        expect(search).to_be_focused()
        assert search.evaluate("element => getComputedStyle(element.closest('.search-turns-box')).boxShadow !== 'none'")
        expect(page.locator(".search-turns-box .search-icon")).to_have_count(1)
        search.fill("no-match")
        expect(page.locator(".metrics-table-panel tbody tr")).to_have_count(0)
        _metrics_capture(page, f"toolbar-focused-{width}")
        clear = page.locator("#metrics-search-clear")
        clear.focus()
        clear.press("Enter")
        expect(search).to_have_value("")
        expect(search).to_be_focused()
        expect(page.locator(".metrics-table-panel tbody tr")).to_have_count(4)
        live = page.locator("#metrics-live")
        expect(live).to_have_attribute("role", "switch")
        live.focus()
        assert page.locator(".metrics-switch-track").evaluate("element => parseFloat(getComputedStyle(element).outlineWidth)") >= 2
        live.press("Space")
        expect(live).not_to_be_checked()
        with page.expect_response(re.compile(r"/metrics/summary\?")):
            live.press("Space")
        expect(live).to_be_checked()
        page.get_by_role("combobox", name="Session lifecycle").select_option("s1")
        assert page.locator(".metrics-session-chip span").evaluate("element => element.getBoundingClientRect().width") <= 280
        _metrics_capture(page, f"toolbar-session-{width}")
        page.get_by_role("button", name="JEV: Disabled").click()
        dialog = page.get_by_role("dialog", name="JEV Routing Configuration")
        dialog.get_by_role("tab", name="Configure").click()
        dialog.locator("#jev-config-mode").select_option("enforce")
        # Save and Apply is the only way out: no clipboard export, no endpoint field.
        expect(dialog.locator("#jev-config-export")).to_have_count(0)
        expect(dialog.get_by_text("Saved to agent environment (run/.env)")).to_be_visible()
        dialog.locator("#jev-config-mode").select_option("off")
        dialog.get_by_role("tab", name="Test Routing").click()
        expect(dialog.get_by_label("TypeSafe endpoint", exact=True)).to_have_count(0)
        expect(dialog.get_by_label("Entry Point", exact=True)).to_have_value("session_start")
        expect(dialog.get_by_role("button", name="Save Changes")).to_be_hidden()
        key = dialog.get_by_label("TYPESAFE_API_KEY", exact=True)
        expect(key).to_have_attribute("type", "password")
        key.fill("browser-test-key")
        dialog.get_by_role("button", name="Show key").click()
        expect(key).to_have_attribute("type", "text")
        dialog.get_by_role("button", name="Hide key").click()
        expect(key).to_have_attribute("type", "password")
        dialog.get_by_label("Role", exact=True).select_option("curator")
        dialog.get_by_label("Entry Point", exact=True).select_option("subagent_phase")
        prompt = dialog.get_by_label("User Prompt / Task", exact=True)
        prompt.fill("Write a distributed Raft consensus algorithm")
        dialog.get_by_role("button", name="Simulate JEV Routing").click()
        expect(dialog.locator(".jev-decision-banner > strong")).to_have_text("ds/deepseek-v4-pro")
        expect(dialog.get_by_role("meter", name="Confidence", exact=True)).to_have_attribute("aria-valuenow", "94")
        expect(dialog.get_by_role("meter", name="ds/deepseek-v4-pro", exact=True)).to_have_attribute("aria-valuenow", "97")
        expect(dialog.locator("progress")).to_have_count(0)
        expect(dialog.locator(".jev-probability-row")).to_have_count(2)
        expect(dialog.locator(".jev-test-status")).to_contain_text("No metrics recorded")
        _metrics_capture(page, f"jev-sandbox-{width}")
        dialog.locator(".jev-wire-inspector").scroll_into_view_if_needed()
        _metrics_capture(page, f"jev-sandbox-results-{width}")
        dialog.locator(".jev-wire-inspector summary").click()
        expect(dialog.locator(".jev-wire-inspector")).to_contain_text("Outgoing Payload")
        expect(dialog.locator(".jev-wire-inspector")).to_contain_text("Raw JEV Response")
        assert dialog.locator("img").count() == 0
        assert page.evaluate("window.sandboxInjected") is None
        assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
        assert dialog.locator("#jev-pane-sandbox").evaluate("""pane => {
            const bounds = pane.getBoundingClientRect();
            return [...pane.querySelectorAll('input, select, textarea, [role="meter"], .jev-decision-banner, .jev-probabilities')]
                .every(element => {
                    const rect = element.getBoundingClientRect();
                    return rect.width >= 100 && rect.left >= bounds.left && rect.right <= bounds.right
                        && element.scrollWidth <= element.clientWidth;
                });
        }""")
        assert dialog.locator("#jev-pane-sandbox button").evaluate_all("""buttons => buttons
            .filter(button => button.getClientRects().length)
            .every(button => button.scrollWidth <= button.clientWidth)""")
        prompt.fill("Fail deliberately")
        dialog.get_by_role("button", name="Simulate JEV Routing").click()
        # A 401 names the credential problem instead of reading like an outage.
        expect(dialog.locator(".jev-test-status")).to_contain_text("TypeSafe rejected the API key")
        expect(dialog.locator(".jev-test-results")).to_be_hidden()
        expect(dialog.get_by_role("button", name="Simulate JEV Routing")).to_be_enabled()
        prompt.fill("Retry the routing decision")
        dialog.get_by_label("TYPESAFE_API_KEY", exact=True).press("Enter")
        expect(dialog.locator(".jev-decision-banner > strong")).to_have_text("ds/deepseek-v4-pro")
        assert len(calls) == 3 and env_file.read_bytes() == env_before
        wire = dialog.locator(".jev-wire-inspector summary")
        wire.focus()
        wire.press("Tab")
        expect(dialog.get_by_role("button", name="Close dialog")).to_be_focused()
        dialog.get_by_role("button", name="Close dialog").press("Shift+Tab")
        expect(wire).to_be_focused()
        tab = dialog.get_by_role("tab", name="Test Routing")
        tab.focus()
        tab.press("ArrowLeft")
        expect(dialog.get_by_role("tab", name="Configure")).to_be_focused()
        page.keyboard.press("ArrowRight")
        pending = []
        page.route("**/metrics/test-route", lambda route: pending.append(route))
        dialog.get_by_role("button", name="Simulate JEV Routing").click()
        expect(dialog.get_by_role("button", name="Simulating")).to_be_disabled()
        dialog.get_by_role("button", name="Cancel request").click()
        expect(dialog.locator(".jev-test-status")).to_contain_text("Request cancelled")
        expect(dialog.get_by_role("button", name="Simulate JEV Routing")).to_be_enabled()
        for route in pending:
            route.abort()
        page.unroute("**/metrics/test-route")
        page.emulate_media(reduced_motion="reduce")
        assert float(dialog.locator(".jev-test-run").evaluate("element => getComputedStyle(element).transitionDuration").removesuffix("s")) <= 0.00001
        page.once("dialog", lambda confirmation: confirmation.dismiss())
        dialog.press("Escape")
        expect(dialog).to_be_visible()
        page.once("dialog", lambda confirmation: confirmation.accept())
        dialog.press("Escape")
        expect(dialog).to_be_hidden()
        expect(page.locator("#jev-test-key")).to_have_value("")
        assert len(calls) == 3 and env_file.read_bytes() == env_before
        connection = connect(database)
        assert "\n".join(connection.iterdump()) == before
        connection.close()
        assert errors == []
        page.get_by_role("button", name="JEV: Disabled").click()
        dialog.get_by_role("tab", name="Test Routing").click()
        dialog.get_by_label("User Prompt / Task", exact=True).fill("Sandbox-only edits stay request-local")
        confirmations = []
        page.on("dialog", lambda confirmation: (confirmations.append(confirmation.message), confirmation.dismiss()))
        dialog.press("Escape")
        expect(dialog).to_be_hidden()
        assert confirmations == [] and env_file.read_bytes() == env_before

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
        expect(picker.locator('option[value="s1"]')).to_contain_text("worker a ·")
        expect(picker.locator('option[value="s2"]')).to_contain_text("worker a ·")
        picker.select_option("s1")
        expect(page.locator(".metrics-session-chip")).to_contain_text("Session: s1")
        rows = page.locator(".metrics-table-panel .runs tbody tr")
        expect(rows).to_have_count(3)
        routine = rows.filter(has_text="72%")
        thread = page.locator(".metrics-thread-card").filter(has=page.locator("tr", has_text="72%"))
        expect(routine).to_contain_text("Explain **WAL**")
        expect(routine.locator(".metrics-topic-title")).to_have_text("initial request")
        expect(thread.locator(".mth-title")).to_contain_text("Explain **WAL** and `SQLite`.")
        # The thread header carries the Slack origin; turn rows do not repeat it.
        expect(routine.locator(".metrics-conversation-badge")).to_have_count(0)
        expect(thread.locator(".metrics-thread-head").get_by_role("link", name="Slack #eng-team", exact=False)).to_have_attribute(
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
        detail.screenshot(path=str(METRICS_CAPTURES / "turn-io.png"))
        detail.locator('[data-tab="technical"]').click()
        detail.locator(".panel-usage").scroll_into_view_if_needed()
        detail.screenshot(path=str(METRICS_CAPTURES / "unified-breakdown.png"))
        detail.get_by_role("button", name="Close").click()
        picker.select_option("s2")
        expect(rows).to_have_count(1)
        expect(page.locator(".metrics-session-chip")).to_contain_text("Session: s2")
        _metrics_capture(page, "lifecycle-picker")

    def test_metrics_navigation_filters_drilldown_and_refresh(self, webapp, page):
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        _agent(page, webapp)
        page.get_by_role("link", name="metrics & routing", exact=True).click()
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        expect(page.locator(".metrics-tile")).to_have_count(3)
        expect(page.locator(".tile-input .tile-sub")).to_have_text("100 (13.9% cache read)")
        expect(page.locator(".tile-input .tile-badge, .tile-output .tile-badge")).to_have_count(0)
        expect(page.locator(".metrics-health-strip .is-warn")).to_have_text("coverage2 exact · 1 derived · 1 unknown")
        rows = page.locator(".metrics-table-panel .runs tbody tr")
        expect(rows).to_have_count(4)
        expect(rows.filter(has_text="Router Decision")).to_have_count(2)
        expect(rows.filter(has_text="Sticky Session")).to_have_count(1)
        expect(rows.filter(has_text="Fallback")).to_have_count(1)
        expect(rows.filter(has_text="Fallback").locator("td").nth(3)).to_have_text("—")
        _metrics_capture(page, "metrics-overview")
        routine = rows.filter(has_text="72%")
        expect(routine).to_contain_text("100 tok")
        routine.click()
        detail = page.get_by_role("dialog", name="Turn detail")
        detail.locator('[data-tab="technical"]').click()
        expect(detail).to_contain_text("ds/deepseek-flash")
        expect(detail).to_contain_text("deepseek-flash")
        detail.screenshot(path=str(METRICS_CAPTURES / "turn-detail-tables.png"))
        page.locator(".detail-nav-tabs [data-tab=technical]").click()
        expect(page.locator(".panel-routing")).to_be_hidden()
        expect(page.locator(".panel-usage")).to_be_visible()
        detail.screenshot(path=str(METRICS_CAPTURES / "turn-detail-usage-tab.png"))
        page.locator(".detail-nav-tabs [data-tab=overview]").click()
        expect(page.locator(".panel-routing")).to_be_visible()
        expect(page.locator(".panel-usage")).to_be_hidden()
        detail.screenshot(path=str(METRICS_CAPTURES / "turn-detail-routing-tab.png"))
        page.locator(".detail-nav-tabs [data-tab=technical]").click()
        expect(page.locator(".panel-routing")).to_be_hidden()
        expect(page.locator(".panel-usage")).to_be_visible()
        _metrics_capture(page, "metrics-dashboard")
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
        assert box["width"] <= 1320
        backdrop_padding_x = (1440 - box["width"]) / 2
        backdrop_padding_y = (900 - box["height"]) / 2
        assert backdrop_padding_x >= 48
        assert backdrop_padding_y >= 36

        # Check content is rendered and not blank
        expect(detail.locator(".metrics-io-head").first).to_contain_text("Turn conversation transcript")
        expect(detail.locator(".io-block-user, .io-block-system")).to_contain_text("sleep cycle")
        expect(detail.locator(".io-block-assistant")).to_contain_text("sleep cycle for this agent team")

        # Save screenshot for visual inspection
        METRICS_CAPTURES.mkdir(parents=True, exist_ok=True)
        artifact_dir = METRICS_CAPTURES
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
        page.locator("#metrics-refresh").click()
        expect(page.locator("#metrics-refresh")).to_be_disabled()
        expect(page.locator("#metrics-refresh")).to_have_attribute("aria-busy", "true")
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
        expect(page.locator(".metrics-thread-card").first.locator("thead th")).to_have_text(
            ["Turn", "Step", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"])
        page.get_by_role("button", name="Flat", exact=True).click()
        expect(page.locator(".metrics-table-panel thead th")).to_have_text(
            ["When", "Session / Topic", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"])
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
        detail.screenshot(path=str(METRICS_CAPTURES / f"overhaul-overview-{width}.png"))
        detail.locator('[data-tab="technical"]').click()
        detail.screenshot(path=str(METRICS_CAPTURES / f"overhaul-technical-{width}.png"))
        detail.get_by_role("button", name="Close", exact=True).click()
        page.get_by_role("combobox", name="Session lifecycle").select_option("s1")
        expect(page.locator(".metrics-session-chip")).to_have_count(1)
        expect(rows).to_have_count(3)
        page.get_by_role("button", name="Clear session filter").click()
        expect(page.locator(".metrics-session-chip")).to_be_hidden()
        expect(rows).to_have_count(4)
        page.get_by_role("searchbox", name="Search loaded turns").fill("no-match")
        expect(rows).to_have_count(0)
        page.locator("#metrics-live").uncheck()
        expect(page.locator("#metrics-live")).not_to_be_checked()
        with page.expect_response(re.compile(r"/metrics/summary\?")):
            page.locator("#metrics-live").check()
        expect(page.locator("#metrics-live")).to_be_checked()
        page.get_by_role("combobox", name="Time range").select_option("744")
        expect(page.get_by_role("combobox", name="Time range")).to_have_value("744")
        _metrics_capture(page, f"overhaul-layout-{width}")

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

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    @pytest.mark.parametrize("shape", ["multiline", "wrapped-single-line"])
    def test_metrics_tool_cards_are_compact_and_escape_transcript_text(self, webapp, page, monkeypatch, width, shape):
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
        result = "\n".join(f"line {index:03d}: " + "測試 <img src=x onerror=window.metricsInjected=true> " * 3 for index in range(160))
        if shape == "wrapped-single-line":
            result = "wrapped-result-" * 600
        available = result[:4000]
        if "\n" in available:
            available = available[:available.rfind("\n") + 1]
        _seed_transcript(webapp, monkeypatch, "worker-a", [
            json.dumps({"type": "assistant", "timestamp": at, "message": {"content": [{"type": "tool_use", "id": "ui-tool", "name": "Bash", "input": {"command": '<img src=x onerror="window.metricsInjected=true">'}}]}}),
            json.dumps({"type": "user", "timestamp": at, "message": {"content": [{"type": "tool_result", "tool_use_id": "ui-tool", "content": result}]}}),
        ])
        page.set_viewport_size({"width": width, "height": 1000})
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.goto(webapp.agent_url() + "/metrics")
        row = page.locator(".metrics-table-panel tbody tr", has_text="72%")
        expect(row.locator(".metrics-conversation-badge").filter(has_text="tool call")).to_have_count(0)
        row.click()
        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail.locator(".metrics-tool-card")).to_have_count(1)
        assert not detail.locator(".metrics-tool-card").evaluate("el => el.open")
        detail.locator(".metrics-tool-card summary").click()
        blocks = detail.locator(".metrics-tool-body")
        expect(blocks).to_have_count(2)
        expect(blocks.first.locator("pre")).to_contain_text("metricsInjected")
        expect(blocks.first.locator(".metrics-tool-expand")).to_be_hidden()
        result_block = blocks.last
        expect(result_block.locator(".metrics-tool-truncated")).to_contain_text("available preview only")
        expect(result_block.locator(".metrics-tool-truncated")).to_contain_text(f"{len(result.encode('utf-8')):,}")
        expand = result_block.locator(".metrics-tool-expand")
        expect(expand).to_have_text(re.compile("Expand full"))
        expect(expand).to_have_attribute("aria-expanded", "false")
        collapsed = result_block.locator("pre").text_content()
        assert len(collapsed.splitlines()) == (10 if shape == "multiline" else 1)
        collapsed_height = result_block.locator("pre").evaluate("element => element.clientHeight")
        assert collapsed_height <= 220
        copy = result_block.get_by_role("button", name="Copy result", exact=True)
        copy.click()
        expect(result_block.get_by_role("status")).to_have_text("Copied")
        assert page.evaluate("navigator.clipboard.readText()") == available
        expect(copy).to_have_attribute("aria-label", "Copy result")
        _metrics_bounds(page, ".metrics-tool-card, .metrics-tool-code, .metrics-tool-copy, .metrics-tool-expand")
        for block in blocks.all():
            assert block.locator(".metrics-tool-copy").evaluate("element => getComputedStyle(element).position") == "absolute"
        result_block.locator(".metrics-tool-truncated").scroll_into_view_if_needed()
        _metrics_capture(page, f"tool-{shape}-default-{width}")
        expand.focus()
        expand.press("Enter")
        expect(expand).to_have_attribute("aria-expanded", "true")
        expect(expand).to_have_text("Collapse")
        region = result_block.locator("pre")
        assert region.text_content() == available
        if shape == "multiline":
            assert len(region.text_content().splitlines()) > 10
            assert available.endswith("\n") and result.startswith(available)
        assert region.text_content().startswith(collapsed)
        assert region.evaluate("element => element.getBoundingClientRect().height") <= 240
        assert region.evaluate("element => element.scrollHeight > element.clientHeight && getComputedStyle(element).overflowY === 'auto'")
        assert region.evaluate("element => element.scrollWidth <= element.clientWidth")
        copy = result_block.get_by_role("button", name="Copy result", exact=True)
        copy.focus()
        copy.press("Enter")
        expect(result_block.get_by_role("status")).to_have_text("Copied")
        assert page.evaluate("navigator.clipboard.readText()") == region.text_content()
        assert page.evaluate("navigator.clipboard.readText()") != result
        region.evaluate("element => {element.scrollTop = element.scrollHeight}")
        result_block.locator(".metrics-tool-truncated").scroll_into_view_if_needed()
        _metrics_capture(page, f"tool-{shape}-expanded-{width}")
        expand.click()
        expect(expand).to_have_attribute("aria-expanded", "false")
        assert region.text_content() == collapsed
        expect(detail.locator("img")).to_have_count(0)
        assert page.evaluate("window.metricsInjected") is None

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_metrics_hides_estimate_labels_without_changing_backend_flags(self, webapp, page, width):
        from bobi.metrics.store import connect

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        connection = connect(webapp.install.state_dir / "metrics/metrics.db")
        connection.execute("UPDATE usage_measurements SET is_estimated=1 WHERE measurement_id='u0'")
        connection.commit()
        connection.close()
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.locator(".metrics-table-panel tbody tr")).to_have_count(4)
        forbidden = re.compile(r"\best\b|\bestimated\b|token estimate", re.IGNORECASE)
        assert not forbidden.search(page.locator(".metrics-page").inner_text())
        expect(page.locator(".metrics-table-panel .metrics-conversation-badge")).to_have_count(0)
        _metrics_capture(page, f"clean-badges-overview-{width}")
        page.locator(".metrics-table-panel tbody tr", has_text="72%").click()
        detail = page.get_by_role("dialog", name="Turn detail")
        detail.locator('[data-tab="technical"]').click()
        expect(detail.locator(".panel-usage")).to_be_visible()
        assert not forbidden.search(detail.inner_text())
        _metrics_capture(page, f"clean-badges-technical-{width}")
        detail.locator('[data-tab="overview"]').click()
        assert not forbidden.search(detail.inner_text())
        payload = page.evaluate("""async agent => {
            const response = await fetch(`/api/agents/${agent}/metrics/turns/t-routine`,
                {headers: {'x-bobi-webui-token': 'e2e-webapp-token'}});
            return response.json();
        }""", webapp.agent)
        assert any(item["is_estimated"] for item in payload["usage_measurements"])

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_metrics_semantic_headlines_and_deduped_sessions(self, webapp, page, monkeypatch, width):
        from bobi.metrics.store import connect

        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)
        connection = connect(webapp.install.state_dir / "metrics/metrics.db")
        connection.execute("UPDATE sessions SET session_name='curator-curator-05ff2ed8-curator', role='curator', provider_session_id='e2e-curator' WHERE session_id='s1'")
        connection.execute("UPDATE sessions SET provider_session_id='e2e-worker-a' WHERE session_id='s2'")
        connection.execute("UPDATE turns SET trigger_kind=CASE turn_id WHEN 't-routine' THEN 'sleep_cycle' WHEN 't-pro' THEN 'compaction' ELSE 'idle' END WHERE session_id='s1'")
        connection.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        for index, trigger in [(4, "inbox"), (5, "startup")]:
            connection.execute("INSERT INTO turns(turn_id,session_id,turn_index,started_at_us,ended_at_us,status,trigger_kind,is_user_initiated) VALUES(?,'s2',?,?,?,'completed',?,0)",
                ("t-" + trigger, index, started + index * 1_000_000, started + index * 1_000_000 + 900000, trigger))
        connection.commit()
        connection.close()
        messages = [
            "You are the **sleep cycle** for this agent team. You run out-of-band, on a schedule, as a monitor to process recent transcript deltas.",
            "Compaction required for the session",
            "No new events to process",
        ]
        entries = []
        for index, prompt in enumerate(messages):
            stamp = datetime.fromtimestamp((started + index * 1_000_000 + 100_000) / 1_000_000, timezone.utc).isoformat()
            entries.extend([_entry("user", prompt, stamp), _entry("assistant", "Maintenance completed", stamp)])
        _seed_transcript(webapp, monkeypatch, "curator", entries)
        slack_at = datetime.fromtimestamp((started + 3_100_000) / 1_000_000, timezone.utc).isoformat()
        worker_entries = [
            _entry("user", "Event: slack/message\n  Fix metrics toolbar layout\n  conversation: slack:T123:channel:C123:thread:1791360056.16\n  channel_name: eng-team", slack_at),
            _entry("assistant", "Toolbar fixed", slack_at),
        ]
        for index, prompt in [(4, "Process the inbox for incoming events"), (5, "You are an agent in a bobi deployment. Initialize the agent.")]:
            stamp = datetime.fromtimestamp((started + index * 1_000_000 + 100_000) / 1_000_000, timezone.utc).isoformat()
            worker_entries.extend([_entry("user", prompt, stamp), _entry("assistant", "Background event processed", stamp)])
        _seed_transcript(webapp, monkeypatch, "worker-a", worker_entries)
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        page.get_by_role("button", name="Flat", exact=True).click()
        rows = page.locator(".metrics-table-panel tbody tr")
        expect(rows).to_have_count(6)
        titles = rows.locator(".metrics-topic-title")
        expect(titles.filter(has_text="Sleep Cycle & Memory Compaction")).to_have_count(2)
        expect(titles.filter(has_text="Idle Standby (No new events)")).to_have_count(1)
        expect(titles.filter(has_text="Inbox Event Processing")).to_have_count(1)
        expect(titles.filter(has_text="Agent Startup & Initialization")).to_have_count(1)
        expect(rows.locator(".mth-agent-badge", has_text="curator")).to_have_count(3)
        expect(rows.locator(".metrics-kind-tag.kind-system")).to_have_count(5)
        expect(rows.locator(".metrics-kind-tag.kind-user")).to_have_count(1)
        expect(rows.filter(has_text="Fix metrics toolbar layout").locator(".metrics-kind-tag")).to_have_text("user prompt")
        expect(titles.filter(has_text="Fix metrics toolbar layout")).to_have_count(1)
        slack = rows.get_by_role("link", name="Slack #eng-team", exact=False)
        expect(slack).to_have_attribute("href", "https://app.slack.com/client/T123/C123/thread/C123-1791360056.16")
        expect(slack).to_have_attribute("rel", "noopener noreferrer")
        expect(slack).to_have_attribute("target", "_blank")
        expect(slack.locator("svg")).to_have_count(1)
        assert slack.evaluate("element => {const style=getComputedStyle(element); return parseFloat(style.fontWeight) >= 600 && element.getBoundingClientRect().height >= 28}")
        expect(rows.locator(".metrics-conversation-badge")).to_have_count(1)
        expect(rows.locator(".metrics-conversation-badge", has_text=re.compile(r"^(supervised|inbox|user|sleep_cycle|compaction|startup|idle)$"))).to_have_count(0)
        slack.focus()
        expect(slack).to_be_focused()
        row_text = "\n".join(rows.all_inner_texts())
        assert "You are an agent in a bobi deployment" not in row_text
        assert "curator-curator" not in row_text
        assert rows.locator(".metrics-topic-meta").count() == 6
        assert rows.locator(".metrics-conversation-badge", has_text="tool call").count() == 0
        geometry = rows.locator(".metrics-topic-cell").evaluate_all("""cells => cells.map(cell => {
            const title = cell.querySelector('.metrics-topic-title');
            const meta = cell.querySelector('.metrics-topic-meta');
            const outer = cell.getBoundingClientRect();
            const top = title.getBoundingClientRect();
            const bottom = meta.getBoundingClientRect();
            return {width: top.width, font: parseFloat(getComputedStyle(title).fontSize),
                readable: top.width >= 160 && top.height >= 14,
                twoLines: bottom.top >= top.bottom - 1,
                inside: top.left >= outer.left && top.right <= outer.right + 1};
        })""")
        assert all(item["readable"] and item["twoLines"] and item["inside"] and item["font"] >= 12 for item in geometry), geometry
        picker = page.locator("#metrics-session")
        expect(picker.locator("option")).to_have_count(3)
        labels = picker.locator("option").all_text_contents()
        assert all("curator-curator" not in label for label in labels)
        with page.expect_response(re.compile(r"/metrics/summary\?")):
            page.locator("#metrics-refresh").click()
        expect(picker.locator("option")).to_have_count(3)
        assert picker.locator("option").all_text_contents() == labels
        page.locator(".metrics-table-panel").scroll_into_view_if_needed()
        _metrics_capture(page, f"recent-turns-{width}")
        def unsafe_and_internal_origins(route):
            response = route.fetch()
            payload = response.json()
            sources = ["supervised", "inbox", "slack"]
            for index, turn in enumerate(payload["turns"]):
                turn.setdefault("conversation", {})["origin"] = {
                    "source": sources[index % len(sources)], "channel_name": "eng-team",
                    "url": "javascript:window.metricsInjected=true", "thread_id": "1791360056.16",
                }
            route.fulfill(response=response, json=payload)
        page.route("**/metrics/turns?**", unsafe_and_internal_origins)
        with page.expect_response(re.compile(r"/metrics/turns\?")):
            page.locator("#metrics-refresh").click()
        expect(rows).to_have_count(6)
        expect(rows.locator(".metrics-conversation-badge")).to_have_count(0)
        expect(rows.locator('a[href^="javascript:"]')).to_have_count(0)
        assert page.evaluate("window.metricsInjected") is None
        _metrics_capture(page, f"no-unsafe-or-internal-origin-tags-{width}")

    @pytest.mark.parametrize("width", [1440, 1100, 700])
    def test_jev_editor_save_validation_readback_and_reset(self, webapp, page, width, monkeypatch):
        from bobi import paths

        # The roster below uses gateway routing ids; without a gateway the save names the fix.
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", "https://gateway.invalid/v1")
        from tests.metrics.test_routing import concise_config

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        env_file = paths.env_path(webapp.install.repo_path)
        env_file.write_text("UNRELATED_SETTING=preserved\n# BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        before = env_file.read_bytes()
        requests = []
        page.on("request", lambda request: requests.append((request.method, request.url, request.post_data)))
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(webapp.agent_url() + "/metrics")
        page.get_by_role("button", name="JEV: Disabled").click()
        dialog = page.get_by_role("dialog", name="JEV Routing Configuration")
        dialog.get_by_role("tab", name="Configure").click()
        editor = dialog.locator("#jev-pane-guide")
        mode = editor.locator("#jev-config-mode")
        candidates = editor.locator("#jev-config-candidates")
        instructions = editor.locator("#jev-config-instructions")
        roles = editor.locator('input[name="jev-role"]')
        mode.select_option("enforce")
        while editor.locator(".jev-candidate-remove").count():
            editor.locator(".jev-candidate-remove").first.click()
        dialog.locator("#jev-config-save").click()
        expect(dialog.locator(".jev-save-msg")).to_contain_text(re.compile("candidate", re.I))
        candidates.fill("ds/deepseek-flash")
        candidates.press("Enter")
        candidates.fill("ds/deepseek-v4-pro")
        editor.locator("#jev-config-candidate-add").click()
        candidates.fill("ds/deepseek-flash")
        candidates.press("Enter")
        expect(editor.locator(".jev-candidate-tag code")).to_have_text(["ds/deepseek-flash", "ds/deepseek-v4-pro"])
        # The control model is picked from the candidates, so it cannot drift off the list.
        control = editor.locator("#jev-config-control")
        expect(control.locator("option")).to_have_text(["ds/deepseek-flash", "ds/deepseek-v4-pro"])
        control.select_option("ds/deepseek-v4-pro")
        expect(editor.locator(".jev-candidate-tag").nth(1)).to_contain_text("control")
        editor.get_by_role("button", name="Remove ds/deepseek-v4-pro").click()
        expect(control).to_have_value("ds/deepseek-flash")
        candidates.fill("ds/deepseek-v4-pro")
        candidates.press("Enter")
        # Native models are picked from a filtered list; nothing to type, nothing to misspell.
        picker = editor.locator("#jev-model-options")
        expect(picker.locator(".jev-model-option").first).to_have_text("opus")
        editor.locator("#jev-model-filter").fill("haiku-4")
        expect(picker.locator(".jev-model-option")).to_have_text(["claude-haiku-4-5"])
        picker.get_by_role("option", name="claude-haiku-4-5").click()
        expect(picker.get_by_role("option", name="claude-haiku-4-5")).to_have_attribute("aria-selected", "true")
        expect(editor.locator(".jev-candidate-tag code").last).to_have_text("claude-haiku-4-5")
        picker.get_by_role("option", name="claude-haiku-4-5").click()
        expect(editor.locator(".jev-candidate-tag code")).to_have_text(["ds/deepseek-flash", "ds/deepseek-v4-pro"])
        editor.locator("#jev-model-filter").fill("haikuuu")
        expect(editor.locator(".jev-model-empty")).to_be_visible()
        editor.locator("#jev-model-filter").fill("")
        candidates.fill("cx/gpt-5.6-lunna")
        candidates.press("Enter")
        expect(editor.locator(".jev-candidate-tag.is-unlisted")).to_have_text(re.compile("cx/gpt-5.6-lunna"))
        editor.get_by_role("button", name="Remove cx/gpt-5.6-lunna").click()
        expect(editor.locator("#jev-mode-hint")).to_contain_text("fall back to the control model")
        expect(editor.locator("#jev-config-egress")).to_have_value("none")
        editor.locator("#jev-config-egress").select_option("redacted")
        expect(editor.locator("#jev-egress-hint")).to_contain_text("secrets, credentials and local paths removed")
        for role in roles.all():
            role.uncheck()
        dialog.locator("#jev-config-save").click()
        expect(dialog.locator(".jev-save-msg")).to_contain_text(re.compile("role", re.I))
        editor.locator('input[name="jev-role"][value="engineer"]').check()
        instructions.fill("")
        dialog.locator("#jev-config-save").click()
        expect(dialog.locator(".jev-save-msg")).to_contain_text(re.compile("instruction", re.I))
        assert not [item for item in requests if item[0] == "POST" and item[1].endswith("/metrics/jev-config")]
        assert env_file.read_bytes() == before
        instructions.fill("Use Flash for simple changes; Pro for the agent's complex coding. <img src=x onerror=window.editorInjected=true>")
        _metrics_bounds(page, ".jev-config-modal, #jev-pane-guide select, #jev-pane-guide textarea, #jev-pane-guide button, #jev-config-candidates, #jev-config-control")
        assert editor.locator("textarea").evaluate_all("elements => elements.every(element => element.getBoundingClientRect().width >= 180)")
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/metrics/jev-config")) as saved:
            dialog.locator("#jev-config-save").click()
        assert saved.value.status == 200
        payload = saved.value.json()
        assert payload["enabled"] and payload["mode"] == "enforce"
        assert payload["candidate_models"] == ["ds/deepseek-flash", "ds/deepseek-v4-pro"]
        assert payload["roles"] == ["engineer"] and payload["instructions"] == instructions.input_value()
        assert payload["prompt_egress"] == "redacted" and payload["brain"] == "auto"
        assert payload["restart_required"] and payload["applies_on"] == "agent_restart"
        expect(dialog.locator(".jev-save-msg")).to_contain_text(re.compile("restart", re.I))
        assert not [item for item in requests if item[0] == "POST" and item[1].endswith("/restart")]
        assert "UNRELATED_SETTING=preserved" in env_file.read_text()
        assert page.evaluate("window.editorInjected") is None and dialog.locator("img").count() == 0
        _metrics_capture(page, f"jev-save-{width}")
        editor.locator("#jev-config-delete").scroll_into_view_if_needed()
        _metrics_capture(page, f"editor-actions-{width}")
        dialog.press("Escape")
        page.reload()
        page.get_by_role("button", name="JEV: Enforce").click()
        dialog.get_by_role("tab", name="Configure").click()
        expect(instructions).to_have_value(payload["instructions"])
        expect(mode).to_have_value("enforce")
        editor.locator("#jev-config-delete").click()
        expect(dialog.locator("#jev-config-confirm")).to_be_visible()
        confirmation = dialog.locator(".jev-confirm-card")
        confirmation.evaluate("element => Promise.all(element.getAnimations().map(animation => animation.finished))")
        assert confirmation.evaluate("element => getComputedStyle(element).opacity") == "1"
        _metrics_bounds(page, ".jev-confirm-card, .jev-confirm-card button")
        assert confirmation.evaluate("element => {const bounds=element.getBoundingClientRect(); return bounds.top >= 0 && bounds.bottom <= innerHeight}")
        _metrics_capture(page, f"jev-reset-confirm-{width}")
        dialog.locator(".jev-confirm-btn-cancel").click()
        assert not [item for item in requests if item[0] == "DELETE"]
        expect(editor.locator("#jev-config-delete")).to_be_focused()
        page.emulate_media(reduced_motion="reduce")
        editor.locator("#jev-config-delete").click()
        assert float(confirmation.evaluate("element => getComputedStyle(element).animationDuration").removesuffix("s")) <= 0.00001
        with page.expect_response(lambda response: response.request.method == "DELETE" and response.url.endswith("/metrics/jev-config")) as deleted:
            dialog.locator("#jev-config-confirm").click()
        assert deleted.value.status == 200 and not deleted.value.json()["enabled"]
        assert deleted.value.json()["mode"] == "off"
        assert not deleted.value.json()["raw_json"]
        assert not any(line.startswith("# BOBI_METRICS_EXPERIMENT_JSON=") for line in env_file.read_text().splitlines())
        assert "BOBI_METRICS_EXPERIMENT_JSON=\n" in env_file.read_text()
        assert "UNRELATED_SETTING=preserved" in env_file.read_text()
        _metrics_capture(page, f"jev-reset-{width}")
        dialog.press("Escape")
        page.reload()
        expect(page.get_by_role("button", name="JEV: Disabled")).to_be_visible()
        assert any(item[0] == "GET" and item[1].endswith("/metrics/jev-config") for item in requests)
        page.get_by_role("button", name="JEV: Disabled").click()
        dialog.get_by_role("tab", name="Configure").click()
        expect(editor.locator(".jev-candidate-tag")).to_have_count(0)

    def test_metrics_unavailable_empty_and_untrusted_model_text(self, webapp, page):
        page.goto(webapp.agent_url() + "/metrics")
        expect(page.get_by_role("status")).to_contain_text("metrics database is not ready")
        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        page.locator("#metrics-refresh").click()
        expect(page.locator(".metrics-tile strong").first).to_have_text("720")
        from bobi.metrics.store import connect

        connection = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        payload = '<img src=x onerror="window.metricsInjected=true">'
        connection.execute("UPDATE router_decisions SET model_selected=?", (payload,))
        connection.commit()
        connection.close()
        page.locator("#metrics-refresh").click()
        expect(page.locator(".metrics-page")).to_contain_text(payload)
        assert page.locator(".metrics-page img").count() == 0
        assert page.evaluate("window.metricsInjected") is None
        page.goto(webapp.agent_url() + "/metrics?session=absent")
        expect(page.locator(".metrics-page")).to_contain_text("No recorded turns in this window.")
        expect(page.locator(".metrics-tile strong").first).to_have_text("not recorded")

    def test_metrics_in_thread_turn_navigation_and_wide_modal(self, webapp, page):
        started = int((time.time() - 60) * 1_000_000)
        seed_dashboard(webapp.install.repo_path, started)

        page.set_viewport_size({"width": 1440, "height": 1200})
        page.goto(webapp.agent_url() + "/metrics")
        cards = page.locator(".metrics-thread-card")
        expect(cards).to_have_count(2)
        # Agent and role live on the thread header only; rows never repeat them.
        expect(page.locator(".metrics-thread-head .mth-agent-badge")).to_have_count(2)
        expect(page.locator(".mth-body .mth-agent-badge")).to_have_count(0)
        expect(page.locator(".metrics-thread-head .metrics-kind-tag")).to_have_count(2)
        head = cards.first.locator(".metrics-thread-head")
        toggle = head.get_by_role("button", name="Toggle thread")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(cards.first.locator(".mth-body")).to_be_hidden()
        toggle.click()
        expect(cards.first.locator(".mth-body")).to_be_visible()
        page.locator(".metrics-table-panel").scroll_into_view_if_needed()
        _metrics_capture(page, "threads-1440")
        rows = page.locator(".metrics-table-panel .runs tbody tr")
        expect(rows).to_have_count(4)

        routine_row = rows.filter(has_text="72%")
        routine_row.click()

        detail = page.get_by_role("dialog", name="Turn detail")
        expect(detail).to_be_visible()

        box = detail.bounding_box()
        assert box is not None
        assert box["width"] >= 1200

        prev_btn = detail.locator(".td-nav-prev")
        next_btn = detail.locator(".td-nav-next")
        counter = detail.locator(".td-nav-counter")
        expect(prev_btn).to_be_disabled()
        expect(next_btn).to_be_enabled()
        expect(counter).to_have_text("Turn 1 of 3")

        next_btn.click()
        expect(counter).to_have_text("Turn 2 of 3")
        expect(prev_btn).to_be_enabled()
        expect(next_btn).to_be_enabled()

        next_btn.click()
        expect(counter).to_have_text("Turn 3 of 3")
        expect(prev_btn).to_be_enabled()
        expect(next_btn).to_be_disabled()

        prev_btn.click()
        expect(counter).to_have_text("Turn 2 of 3")

        detail.get_by_role("button", name="Close").click()
        expect(detail).to_be_hidden()

        fallback_row = rows.filter(has_text="Fallback")
        fallback_row.click()
        expect(detail).to_be_visible()

        expect(prev_btn).to_be_disabled()
        expect(next_btn).to_be_disabled()
        expect(counter).to_have_text("Turn #3")

        detail.get_by_role("button", name="Close").click()

        # Flat view: "#n" only where the session has more than one loaded turn.
        page.get_by_role("button", name="Flat", exact=True).click()
        flat = page.locator(".metrics-table-panel .runs tbody tr")
        expect(flat).to_have_count(4)
        expect(flat.locator(".metrics-turn-index")).to_have_count(3)
        expect(flat.locator(".metrics-session-chip-inline")).to_have_count(4)
        _metrics_capture(page, "flat-1440")

    @pytest.mark.parametrize("width", [1440, 700])
    def test_jev_card_prices_routing_against_its_candidate_envelope(self, webapp, page, width):
        from bobi.metrics.store import connect

        seed_dashboard(webapp.install.repo_path, int((time.time() - 60) * 1_000_000))
        page.set_viewport_size({"width": width, "height": 1100})
        with page.expect_response(re.compile(r"/metrics/summary\?")) as response:
            page.goto(webapp.agent_url() + "/metrics")
        savings = response.value.json()["routing"]["savings"]
        card = page.locator(".jev-summary-card")
        net = card.locator(".jev-kpi.is-saved")
        expect(net.locator(".jev-kpi-value")).to_have_text(re.compile(r"^\$0\.0000\d+$"))
        expect(net).to_contain_text(f"{savings['saved_pct']:.1f}% cheaper than always ds/deepseek-v4-pro")
        expect(card.locator(".jev-kpi")).to_have_count(4)
        expect(card.locator(".jev-kpi").nth(1)).to_contain_text("3 of 4 turns routed")
        expect(card.locator(".jev-dist-seg")).to_have_count(2)
        # Real model names and shares, busiest first; no invented tier labels.
        expect(card.locator(".jev-dist-item")).to_have_text(["ds/deepseek-v4-pro2 turns (67%)", "ds/deepseek-flash1 turn (33%)"])
        table = card.locator(".jev-table")
        expect(table.locator("thead th")).to_have_text(["Model", "Share / Turns", "Actual Spend", "Baseline Cost (Flagship)", "Net Savings"])
        # One flat table: a row per picked model, no per-router divider rows.
        expect(table.locator("tbody tr")).to_have_count(2)
        expect(table.locator("tbody .jev-cell-model code")).to_have_text(["ds/deepseek-v4-pro", "ds/deepseek-flash"])
        expect(table.locator("tbody td:nth-child(2)")).to_have_text(["67% / 2", "33% / 1"])
        expect(card).not_to_contain_text(re.compile("Economy|Balanced|Tier"))
        expect(table.locator("tbody tr").nth(0).locator(".jev-cell-saved")).to_have_text("—(Baseline)")
        expect(table.locator("tfoot")).to_contain_text("100% / 3")
        expect(table.locator("tfoot")).to_contain_text(f"({savings['saved_pct']:.1f}%)")
        expect(card.locator(".jev-footnote")).to_contain_text("Baseline Cost (Flagship) prices every turn on the most expensive model")
        expect(card).not_to_contain_text("not measurable")
        # Telemetry health is a single strip, not a second card beside the value.
        expect(page.locator(".metrics-health-strip")).to_contain_text("coverage")
        expect(page.get_by_text("Telemetry Health & Coverage")).to_have_count(0)
        card.scroll_into_view_if_needed()
        _metrics_capture(page, f"jev-savings-{width}")

        conn = connect(webapp.install.repo_path / "state/metrics/metrics.db")
        conn.execute("UPDATE router_decisions SET candidate_models_json='[\"ds/deepseek-flash\",\"cx/gpt-7-nova\"]' "
                     "WHERE turn_id='t-routine'")
        conn.commit()
        conn.close()
        page.locator("#metrics-refresh").click()
        # An unlisted candidate is named, never priced by guess.
        expect(card.locator(".jev-footnote")).to_contain_text("1 routed turn left out: no list price for cx/gpt-7-nova")


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
