"""Team composition and cache savings (U4).

`GET .../overview` — the identity header's read-only view of what a team IS —
and the `script_cache` block the spend payload gained. Both fold from files on
disk, so both are tested against a real seeded home.
"""

import json

import yaml
from fastapi.testclient import TestClient

from bobi import paths
from bobi.webapp import server
from bobi.webapp.overview import build_overview
from bobi.webapp.savings import script_cache_savings

TOKEN = "overview-token-123"


def _write_agent_yaml(bobi_install, **overrides):
    data = {"version": "0.0.1", "agent": "test-agent", "entry_point": "director"}
    data.update(overrides)
    paths.agent_yaml_path(bobi_install.repo_path).write_text(yaml.dump(data))


def _write_agent_md(bobi_install, text):
    (paths.package_dir(bobi_install.repo_path) / "agent.md").write_text(text)


def _write_role(bobi_install, name, body):
    # Roles are discovered as <roles>/<name>/ROLE.md, the shape the installed
    # package image uses — a flat .md is not a role.
    d = paths.roles_dir(bobi_install.repo_path) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "ROLE.md").write_text(body)


def _write_workflow(bobi_install, name):
    d = paths.workflows_dir(bobi_install.repo_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.dump({"name": name, "steps": []}))


def _write_cache_state(bobi_install, monitor, **state):
    d = paths.state_path(bobi_install.repo_path) / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{monitor}.state.json").write_text(json.dumps(state))


def _overview(bobi_install):
    return build_overview(bobi_install.repo_path)


# === Composition ===

class TestOverview:
    def test_description_is_the_first_paragraph_of_agent_md(self, bobi_install):
        _write_agent_md(bobi_install,
                        "# Test Agent\n\nWatches the inbox and files tickets.\n"
                        "\n## Details\n\nNot this paragraph.\n")
        assert _overview(bobi_install)["description"] == \
            "Watches the inbox and files tickets."

    def test_missing_agent_md_costs_the_field_not_the_response(self,
                                                               bobi_install):
        (paths.package_dir(bobi_install.repo_path) / "agent.md").unlink(
            missing_ok=True)
        body = _overview(bobi_install)
        assert body["description"] == ""
        assert body["name"] == bobi_install.agent_name

    def test_roles_carry_their_descriptions_sorted(self, bobi_install):
        # The fixture's image already ships director + engineer.
        _write_role(bobi_install, "reviewer", "# Reviewer\n\nCopy-edits drafts.\n")
        roles = _overview(bobi_install)["roles"]
        assert [r["name"] for r in roles] == ["director", "engineer", "reviewer"]
        by_name = {r["name"]: r for r in roles}
        assert by_name["reviewer"]["description"] == "Copy-edits drafts."
        assert by_name["director"]["description"]

    def test_chat_names_its_service_and_channels(self, bobi_install):
        _write_agent_yaml(bobi_install, chat="slack", services=[
            {"name": "slack", "events": True, "channels": ["C123", "C456"]},
            {"name": "linear", "events": True},
        ])
        body = _overview(bobi_install)
        assert body["chat"] == {"service": "slack",
                                "channels": ["C123", "C456"]}
        assert [s["name"] for s in body["services"]] == ["slack", "linear"]

    def test_chat_service_that_is_not_declared_still_reports_the_service(
            self, bobi_install):
        _write_agent_yaml(bobi_install, chat="slack", services=[])
        assert _overview(bobi_install)["chat"] == {"service": "slack",
                                                   "channels": []}

    def test_no_chat_configured(self, bobi_install):
        _write_agent_yaml(bobi_install)
        assert _overview(bobi_install)["chat"] == {"service": "", "channels": []}

    def test_team_monitors_count_as_scheduled(self, bobi_install):
        before = _overview(bobi_install)["automations"]["monitors"]
        _write_agent_yaml(bobi_install, monitors=[
            {"name": "inbox-watch", "interval": "15m", "command": "true",
             "description": "watch"},
        ])
        assert _overview(bobi_install)["automations"]["monitors"] == before + 1

    def test_opting_out_of_an_inherited_default_unschedules_it(self,
                                                                bobi_install):
        # `enabled: false` on a team's own entry is an opt-out of a framework
        # default, not a paused monitor of its own. Counting all_monitors() by
        # its `enabled` flag would report this one as still scheduled — the
        # whole reason the count goes through the scheduler's own pair.
        # `test-check` is the default the fixture's image installs.
        before = _overview(bobi_install)["automations"]
        assert before["monitors"] == 1
        _write_agent_yaml(bobi_install, monitors=[
            {"name": "test-check", "interval": "1h", "command": "true",
             "description": "off", "enabled": False},
        ])
        after = _overview(bobi_install)["automations"]
        assert after["monitors"] == before["monitors"] - 1
        assert after["paused_monitors"] == before["paused_monitors"] + 1

    def test_workflow_count(self, bobi_install):
        # The fixture's image ships one workflow (adhoc).
        before = _overview(bobi_install)["automations"]["workflows"]
        _write_workflow(bobi_install, "triage")
        _write_workflow(bobi_install, "release")
        assert _overview(bobi_install)["automations"]["workflows"] == before + 2

    def test_brain_defaults_are_made_explicit(self, bobi_install):
        _write_agent_yaml(bobi_install)
        brain = _overview(bobi_install)["brain"]
        # An unset brain still runs one, so the page shows which.
        assert brain["kind"] == "claude"
        # But an unset model is the provider's default, not a name to guess.
        assert brain["model"] == ""
        assert brain["max_turns"] == 0
        assert brain["gateway"] is False

    def test_brain_overrides_are_reported(self, bobi_install):
        _write_agent_yaml(bobi_install, brain={
            "kind": "codex", "model": "gpt-5.6", "effort": "high",
            "max_turns": 40})
        brain = _overview(bobi_install)["brain"]
        assert brain == {"kind": "codex", "model": "gpt-5.6", "effort": "high",
                         "max_turns": 40, "gateway": False}

    def test_gateway_is_flagged_without_leaking_the_url(self, bobi_install):
        # A base_url can carry a key; that it is set is the fact worth showing.
        _write_agent_yaml(bobi_install, brain={
            "kind": "claude", "base_url": "https://gw.example/v1?key=secret"})
        body = _overview(bobi_install)
        assert body["brain"]["gateway"] is True
        assert "secret" not in json.dumps(body)

    def test_spend_cap_says_whether_it_is_the_default(self, bobi_install):
        _write_agent_yaml(bobi_install)
        assert _overview(bobi_install)["spend_cap"] == {"value": 50,
                                                        "is_default": True}
        _write_agent_yaml(bobi_install, spend_cap=40)
        assert _overview(bobi_install)["spend_cap"] == {"value": 40,
                                                        "is_default": False}

    def test_entry_role_is_reported(self, bobi_install):
        _write_agent_yaml(bobi_install, entry_point="director")
        assert _overview(bobi_install)["entry_role"] == "director"

    def test_unparseable_agent_yaml_still_answers(self, bobi_install):
        # The page whose job is telling you the agent is in trouble must not
        # 500 because the agent is in trouble.
        paths.agent_yaml_path(bobi_install.repo_path).write_text("{{ not yaml")
        _write_agent_md(bobi_install, "# T\n\nStill readable.\n")
        body = _overview(bobi_install)
        assert body["description"] == "Still readable."
        assert body["services"] == []
        assert body["brain"]["kind"] == "claude"
        assert body["entry_role"] == "manager"       # the framework default


# === Script-cache savings ===

class TestScriptCacheSavings:
    def test_no_scripts_directory(self, bobi_install):
        assert script_cache_savings(bobi_install.repo_path) == {
            "monitors": 0, "cached_runs": 0, "agent_runs": 0,
            "agent_cost_usd": 0.0, "estimated_savings_usd": 0.0,
            "priced_monitors": 0,
        }

    def test_reading_never_creates_the_directory(self, bobi_install):
        script_cache_savings(bobi_install.repo_path)
        assert not (paths.state_path(bobi_install.repo_path)
                    / "scripts").exists()

    def test_savings_use_the_monitors_own_average(self, bobi_install):
        # 4 paid ticks cost $2.00, so an agent tick is worth $0.50 here; the
        # 20 cached ticks therefore saved $10.
        _write_cache_state(bobi_install, "inbox-watch", cached_runs=20,
                           fallback_runs=4, total_agent_cost_usd=2.0)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["cached_runs"] == 20
        assert body["agent_runs"] == 4
        assert body["agent_cost_usd"] == 2.0
        assert body["estimated_savings_usd"] == 10.0
        assert body["priced_monitors"] == 1

    def test_each_monitor_is_priced_separately(self, bobi_install):
        # A cheap monitor's savings must not be priced with an expensive
        # one's bill. Deliberately ASYMMETRIC — the cheap monitor caches far
        # more often — because with equal cached counts a blended fleet
        # average lands on the same total and the test would prove nothing:
        #   per-monitor: 20 × $0.10 + 5 × $1.00        = $7.00
        #   blended:     25 × ($1.10 / 2 runs) = $0.55 = $13.75
        _write_cache_state(bobi_install, "cheap", cached_runs=20,
                           fallback_runs=1, total_agent_cost_usd=0.10)
        _write_cache_state(bobi_install, "dear", cached_runs=5,
                           fallback_runs=1, total_agent_cost_usd=1.00)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["estimated_savings_usd"] == 7.0
        assert body["monitors"] == 2
        assert body["priced_monitors"] == 2

    def test_no_paid_tick_means_no_priced_saving(self, bobi_install):
        # Under subscription auth an agent tick records $0. There is then no
        # basis for a dollar figure, and the cached-run count carries the
        # story instead of a modelled number with nothing under it.
        _write_cache_state(bobi_install, "inbox-watch", cached_runs=50,
                           fallback_runs=2, total_agent_cost_usd=0.0)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["cached_runs"] == 50
        assert body["estimated_savings_usd"] == 0.0
        assert body["priced_monitors"] == 0

    def test_never_cached_monitor_saves_nothing(self, bobi_install):
        _write_cache_state(bobi_install, "always-regen", cached_runs=0,
                           fallback_runs=3, total_agent_cost_usd=1.5)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["estimated_savings_usd"] == 0.0
        assert body["agent_cost_usd"] == 1.5

    def test_corrupt_and_foreign_files_are_skipped(self, bobi_install):
        d = paths.state_path(bobi_install.repo_path) / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "broken.state.json").write_text("{ not json")
        (d / "inbox-watch.sc.sh").write_text("#!/bin/sh\necho hi\n")
        _write_cache_state(bobi_install, "good", cached_runs=4,
                           fallback_runs=1, total_agent_cost_usd=0.4)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["monitors"] == 1
        assert body["estimated_savings_usd"] == 1.6

    def test_garbage_counter_types_are_ignored(self, bobi_install):
        _write_cache_state(bobi_install, "odd", cached_runs="lots",
                           fallback_runs=-2, total_agent_cost_usd=None)
        body = script_cache_savings(bobi_install.repo_path)
        assert body["cached_runs"] == 0
        assert body["agent_runs"] == 0
        assert body["agent_cost_usd"] == 0.0


# === The endpoints ===

def _client():
    app = server.build_app(token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


class TestEndpoints:
    def test_overview_shape(self, bobi_install):
        _write_agent_md(bobi_install, "# T\n\nA test agent.\n")
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/overview")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"name", "description", "roles", "chat", "services",
                             "automations", "brain", "spend_cap", "entry_role"}
        assert body["name"] == bobi_install.agent_name
        assert body["description"] == "A test agent."

    def test_overview_unknown_agent_404s(self, bobi_install):
        assert _client().get("/api/agents/nope/overview").status_code == 404

    def test_spend_carries_the_script_cache_block(self, bobi_install):
        _write_cache_state(bobi_install, "inbox-watch", cached_runs=10,
                           fallback_runs=2, total_agent_cost_usd=1.0)
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        body = r.json()
        assert body["script_cache"]["estimated_savings_usd"] == 5.0
        # Counterfactual dollars never join the bill.
        assert body["total_cost_usd"] == 0.0
        assert body["estimated_cost_usd"] == 0.0

    def test_spend_keeps_its_existing_keys(self, bobi_install):
        body = _client().get(
            f"/api/agents/{bobi_install.agent_name}/spend").json()
        for key in ("total_cost_usd", "sessions_counted", "by_provider",
                    "by_model", "by_session", "by_role", "estimated_cost_usd",
                    "estimated_by_model", "tokens_by_model"):
            assert key in body
