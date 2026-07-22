"""Tests for the Build authoring step — deterministic structure, the
manifest, and a full pour that produces a pack passing validation."""

import asyncio

import pytest
import yaml

from bobi import paths
from bobi.setup import actions, authoring
from bobi.setup.authoring import (
    build_adhoc_yaml,
    build_agent_yaml,
    build_monitors_yaml,
    cadence_to_interval,
    compute_entry_point,
    compute_manifest,
    slug,
)
from bobi.setup.state import SetupState


@pytest.fixture(autouse=True)
def _isolated_bobi_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BOBI_HOME", str(home))


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [e async for e in agen]


def _spec_state(**kw):
    s = SetupState(team_name="triage-bot")
    s.spec.goal = "Triage incoming GitHub issues and route them to owners."
    s.spec.roles = [{"name": "Triage Lead", "responsibility": "classify issues"},
                    {"name": "router", "responsibility": "assign owners"}]
    s.spec.services = [{"name": "github"}, {"name": "slack"}]
    for k, v in kw.items():
        setattr(s.spec, k, v)
    return s


class TestSlugAndEntryPoint:
    def test_slug(self):
        assert slug("Triage Lead!") == "triage-lead"
        assert slug("  multiple   spaces ") == "multiple-spaces"

    def test_entry_point_is_first_role_slug(self):
        assert compute_entry_point(_spec_state()) == "triage-lead"

    def test_goal_only_team_gets_assistant_role(self):
        s = SetupState(team_name="t")
        s.spec.goal = "Do a thing."
        assert compute_entry_point(s) == "assistant"
        assert authoring.normalized_roles(s)[0]["name"] == "assistant"


class TestDeterministicBodies:
    def test_agent_yaml_structure(self):
        cfg = yaml.safe_load(build_agent_yaml(_spec_state()))
        assert cfg["agent"] == "triage-bot"
        assert cfg["entry_point"] == "triage-lead"
        assert cfg["event_server"] == "${BOBI_EVENT_SERVER:-}"
        names = {s["name"] for s in cfg["services"]}
        assert names == {"github", "slack"}
        slack = next(s for s in cfg["services"] if s["name"] == "slack")
        assert slack["credentials"]["bot_token"] == "${SLACK_BOT_TOKEN}"
        assert slack["credentials"]["app_token"] == "${SLACK_APP_TOKEN:-}"
        assert slack["events"] is True

    def test_monitor_role_defaults_to_cheap_model(self):
        # Generated packs put monitor checks on a cheap model by default
        # (#617, #549 Part A). Setup output runs on the default Claude
        # brain, so the alias is always valid.
        cfg = yaml.safe_load(build_agent_yaml(_spec_state()))
        assert cfg["roles"] == {"monitor": {"model": "haiku"}}

    def test_merge_preserves_hand_written_roles(self):
        from bobi.setup.authoring import merge_agent_yaml
        existing = (
            "agent: triage-bot\nentry_point: triage-lead\n"
            "roles:\n  monitor:\n    model: opus\n"
        )
        merged = yaml.safe_load(merge_agent_yaml(existing, _spec_state()))
        assert merged["roles"] == {"monitor": {"model": "opus"}}

    def test_merge_adds_monitor_default_to_existing_roles_block(self):
        # Per-key union (like mcp_servers): a hand-written roles block
        # missing the monitor entry still gains the cheap default
        # (#617 review finding).
        from bobi.setup.authoring import merge_agent_yaml
        existing = (
            "agent: triage-bot\nentry_point: triage-lead\n"
            "roles:\n  reviewer:\n    model: opus\n"
        )
        merged = yaml.safe_load(merge_agent_yaml(existing, _spec_state()))
        assert merged["roles"] == {
            "reviewer": {"model": "opus"},
            "monitor": {"model": "haiku"},
        }

    def test_merge_skips_monitor_default_for_non_claude_brain(self):
        # `haiku` is a Claude alias; injecting it into a codex pack would
        # break every monitor check (#617 review finding).
        from bobi.setup.authoring import merge_agent_yaml
        existing = (
            "agent: triage-bot\nentry_point: triage-lead\n"
            "brain:\n  kind: codex\n"
        )
        merged = yaml.safe_load(merge_agent_yaml(existing, _spec_state()))
        assert "roles" not in merged
        assert merged["brain"] == {"kind": "codex"}

    def test_merge_skips_monitor_default_for_gateway_brain(self):
        # A gateway backend serves its own model names; `haiku` would only
        # work by coincidence (#655).
        from bobi.setup.authoring import merge_agent_yaml
        existing = (
            "agent: triage-bot\nentry_point: triage-lead\n"
            "brain:\n  kind: gateway\n  base_url: http://localhost:4000\n"
        )
        merged = yaml.safe_load(merge_agent_yaml(existing, _spec_state()))
        assert "roles" not in merged
        assert merged["brain"]["kind"] == "gateway"

    def test_venn_services_declare_the_shared_key(self):
        # A team using Venn-backed services must declare venn_api_key so
        # Named start resolves it from the env / .env (else preflight
        # fails "venn — no API key" despite the key being set).
        s = SetupState(team_name="inbox-monitor")
        s.spec.goal = "Watch the inbox."
        s.spec.roles = [{"name": "inbox-monitor", "responsibility": "watch"}]
        s.spec.services = [{"name": "email"}, {"name": "calendar"}]
        cfg = yaml.safe_load(build_agent_yaml(s))
        assert cfg["venn_api_key"] == "${VENN_API_KEY}"

    def test_native_only_team_has_no_venn_key(self):
        cfg = yaml.safe_load(build_agent_yaml(_spec_state()))  # github + slack
        assert "venn_api_key" not in cfg

    def test_chat_slack_writes_chat_and_folds_service(self):
        s = _spec_state()
        s.chat = "slack"
        cfg = yaml.safe_load(build_agent_yaml(s))
        assert cfg["chat"] == "slack"
        names = {sv["name"] for sv in cfg["services"]}
        assert "slack" in names                       # folded in as a service
        slack = next(sv for sv in cfg["services"] if sv["name"] == "slack")
        assert slack["credentials"]["bot_token"] == "${SLACK_BOT_TOKEN}"

    def test_chat_cli_writes_no_chat_key(self):
        s = _spec_state()
        s.chat = "cli"
        cfg = yaml.safe_load(build_agent_yaml(s))
        assert "chat" not in cfg

    def test_agent_yaml_has_no_literal_secret_refs(self):
        # ${VAR} references are not literal secrets.
        text = build_agent_yaml(_spec_state())
        assert "${SLACK_BOT_TOKEN}" in text

    def test_adhoc_workflow_parses_with_real_loader(self, tmp_path):
        from bobi.workflow.schema import load_workflow
        f = tmp_path / "adhoc.yaml"
        f.write_text(build_adhoc_yaml())
        wf = load_workflow(f)
        assert wf.name == "adhoc"

    def test_monitors_yaml_from_autonomous(self):
        s = _spec_state(autonomous=[
            {"description": "Ping me about stale PRs each morning",
             "leash": "notify", "cadence": "1d"},
            {"description": "Watch for failing deploys", "leash": "act",
             "cadence": "when a deploy finishes"}])
        raw = yaml.safe_load(build_monitors_yaml(s))
        mons = raw["monitors"]
        assert mons[0]["interval"] == "1d"
        assert mons[0]["notify"] is True
        # event-shaped cadence falls back to a sane default interval
        assert mons[1]["interval"] == "15m"
        # every record is schema-valid
        from bobi.monitors.schema import Monitor, parse_interval
        for rec in mons:
            Monitor.from_dict(dict(rec))
            parse_interval(rec["interval"])

    def test_cadence_to_interval(self):
        assert cadence_to_interval("15m") == "15m"
        assert cadence_to_interval("every morning") == "15m"
        assert cadence_to_interval("") == "15m"


class TestManifest:
    def test_files_for_full_spec(self):
        s = _spec_state(autonomous=[{"description": "daily digest",
                                     "leash": "notify", "cadence": "1d"}])
        paths = [f.path for f in compute_manifest(s)]
        assert paths == [
            "agent.yaml", "agent.md",
            "roles/triage-lead/ROLE.md", "roles/router/ROLE.md",
            "workflows/adhoc.yaml", "monitors/defaults.yaml"]

    def test_no_monitors_file_without_autonomous(self):
        paths = [f.path for f in compute_manifest(_spec_state())]
        assert "monitors/defaults.yaml" not in paths

    def test_agent_yaml_is_deterministic_others_authored(self):
        m = {f.path: f for f in compute_manifest(_spec_state())}
        assert m["agent.yaml"].deterministic
        assert m["workflows/adhoc.yaml"].deterministic
        assert not m["agent.md"].deterministic
        assert m["agent.md"].user and m["agent.md"].system


class TestAuthorPour:
    def _fake_stream(self):
        async def fake(*, system_prompt, user_prompt, model, cwd):
            # Pretend the model authored prose for this file, in chunks.
            yield "# "
            yield "Generated\n\n"
            yield "You do the work described in the spec.\n"
        return fake

    def test_pour_writes_a_valid_pack(self, tmp_path):
        s = _spec_state(autonomous=[{"description": "daily digest",
                                     "leash": "notify", "cadence": "1d"}])
        events = _run(_collect(authoring.author_pack(
            s, tmp_path, stream_fn=self._fake_stream())))

        pack = paths.agent_source_dir("triage-bot")
        assert (pack / "agent.yaml").exists()
        assert (pack / "roles" / "triage-lead" / "ROLE.md").exists()
        # passes the real validator
        result = actions.validate_team(s, tmp_path)
        assert result["passed"] is True, result["report"]

        # pour events bracket each file with start/end
        starts = [e["path"] for e in events if e["type"] == "file_start"]
        ends = [e["path"] for e in events if e["type"] == "file_end"]
        assert starts == ends
        assert "agent.md" in starts

    def test_create_respects_explicit_source_dir(self, tmp_path):
        # Create's location is the exact source directory. Relative paths are
        # anchored at BOBI_HOME because setup is machine-scoped, not cwd-scoped.
        s = _spec_state()                 # team_name="triage-bot", mode=create
        s.source_dir = "sources/triage-bot"
        _run(_collect(authoring.author_pack(
            s, tmp_path, stream_fn=self._fake_stream())))
        pack = paths.home_dir() / "sources" / "triage-bot"
        assert (pack / "agent.yaml").is_file()
        # the concrete path is persisted exactly.
        assert s.source_dir == str(pack)
        s.team_name = "triage-bot"
        assert actions.team_source_dir(tmp_path, s) == pack

    def test_create_refuses_to_overwrite_an_existing_canonical_source(self, tmp_path):
        # A fresh create must not clobber an existing canonical source tree.
        existing = paths.agent_source_dir("triage-bot")
        existing.mkdir(parents=True)
        (existing / "agent.yaml").write_text("agent: the-original\n")
        s = _spec_state()                 # team_name="triage-bot", mode=create
        with pytest.raises(actions.ActionError, match="already exists"):
            _run(_collect(authoring.author_pack(
                s, tmp_path, stream_fn=self._fake_stream())))
        # the original team's source is untouched
        assert (existing / "agent.yaml").read_text() == "agent: the-original\n"

    def test_create_reauthors_its_own_claimed_folder(self, tmp_path):
        # Re-running build once the team is claimed (source_dir already points at
        # the exact source directory) is fine — not a collision with a
        # different team.
        existing = paths.home_dir() / "sources" / "triage-bot"
        existing.mkdir(parents=True)
        (existing / "agent.yaml").write_text("agent: triage-bot\n")
        s = _spec_state()
        s.source_dir = str(existing)   # already ours
        _run(_collect(authoring.author_pack(
            s, tmp_path, stream_fn=self._fake_stream())))
        assert (existing / "agent.yaml").is_file()   # re-authored, no error

    def test_pour_failure_preserves_original_file_in_open_mode(self, tmp_path):
        # Regression (review F3): a mid-pour failure (llm.stream raises — e.g. the
        # model stalls past the idle timeout) must NOT truncate the user's
        # existing file. In open/modify mode the original agent.md/ROLE.md must
        # survive an authoring failure intact, never be left half-written.
        from bobi.setup import open_mode
        from bobi.setup.llm import LLMError

        src = tmp_path / "sources" / "myteam"
        (src / "roles" / "lead").mkdir(parents=True)
        (src / "agent.yaml").write_text("agent: myteam\nentry_point: lead\n")
        original_md = "# myteam\n\nORIGINAL BASE PROMPT — do not lose me.\n"
        (src / "agent.md").write_text(original_md)
        (src / "roles" / "lead" / "ROLE.md").write_text("# Lead\n\nORIGINAL ROLE.\n")

        s = SetupState()
        s.mode = "open"
        s.source_dir = str(src)
        open_mode.reverse_fill(s, src)

        async def stalling(*, system_prompt, user_prompt, model, cwd):
            yield "# partial"          # one chunk lands on the wire...
            raise LLMError("stalled")  # ...then the call dies mid-pour

        with pytest.raises(LLMError):
            _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=stalling)))

        # The original prose file is intact — not truncated to "# partial".
        assert (src / "agent.md").read_text() == original_md

    def test_pour_strips_wrapping_code_fence(self, tmp_path):
        async def fenced(*, system_prompt, user_prompt, model, cwd):
            yield "```markdown\n# Title\n\nBody text.\n```"

        s = SetupState(team_name="t")
        s.spec.goal = "Do a thing."
        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=fenced)))
        agent_md = (paths.agent_source_dir("t") / "agent.md").read_text()
        assert agent_md.startswith("# Title")
        assert "```" not in agent_md

    def test_role_md_authored_with_agent_md_in_context(self, tmp_path):
        # The just-written agent.md must be threaded into the ROLE.md prompt
        # so roles cohere with the base prompt.
        seen = {}

        def fake():
            async def fn(*, system_prompt, user_prompt, model, cwd):
                if "agent.md" in user_prompt and "ROLE.md" not in user_prompt:
                    yield "# Triage Base\n\nShared base prompt body.\n"
                else:
                    seen["role_prompt"] = user_prompt
                    yield "# Role\n\nYou do the thing.\n"
            return fn

        s = _spec_state()
        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=fake())))
        assert "Shared base prompt body." in seen["role_prompt"]
        assert "align with it" in seen["role_prompt"]

    def test_empty_authored_file_gets_stub(self, tmp_path):
        async def empty(*, system_prompt, user_prompt, model, cwd):
            return
            yield  # pragma: no cover

        s = SetupState(team_name="t")
        s.spec.goal = "Do a thing."
        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=empty)))
        agent_md = (paths.agent_source_dir("t") / "agent.md").read_text()
        assert agent_md.strip()  # non-empty stub, not a blank file


class TestNonLossyMerges:
    """Open/modify mode must never drop content the pack already carries."""

    def test_merge_agent_yaml_unions_services_and_keeps_extra_keys(self):
        existing = (
            "agent: legacy\nversion: 9.9.9\nentry_point: lead\n"
            "context:\n  - rubric.md\n"
            "services:\n  - name: github\n    events: true\n"
            "    credentials:\n      token: ${GH}\n")
        s = _spec_state()                      # spec services: github + slack
        s.team_name, s.chat = "legacy", "slack"
        merged = yaml.safe_load(authoring.merge_agent_yaml(existing, s))
        assert merged["context"] == ["rubric.md"]       # hand-written key kept
        assert merged["version"] == "9.9.9"             # not clobbered to 0.1.0
        names = {x["name"] for x in merged["services"]}
        assert names == {"github", "slack"}             # slack unioned in
        gh = next(x for x in merged["services"] if x["name"] == "github")
        assert gh["credentials"] == {"token": "${GH}"}  # rich entry untouched
        assert merged["entry_point"] == "triage-lead"   # recomputed from spec

    def test_merge_agent_yaml_updates_name_on_rename(self):
        # `agent` is the team name (setup-managed) — a rename must take, even
        # though the existing file already declares the old name.
        existing = "agent: old-name\nversion: 0.1.0\nentry_point: lead\n"
        s = _spec_state()
        s.team_name = "new-name"
        merged = yaml.safe_load(authoring.merge_agent_yaml(existing, s))
        assert merged["agent"] == "new-name"

    def test_merge_agent_yaml_adds_optional_event_server_reference(self):
        existing = "agent: legacy\nversion: 0.1.0\nentry_point: lead\n"
        merged = yaml.safe_load(authoring.merge_agent_yaml(existing, _spec_state()))
        assert merged["event_server"] == "${BOBI_EVENT_SERVER:-}"

    def test_merge_agent_yaml_preserves_existing_event_server(self):
        existing = (
            "agent: legacy\nversion: 0.1.0\nentry_point: lead\n"
            "event_server: https://events.test\n"
        )
        merged = yaml.safe_load(authoring.merge_agent_yaml(existing, _spec_state()))
        assert merged["event_server"] == "https://events.test"

    def test_merge_monitors_unions_by_name(self):
        existing = ("monitors:\n  - name: hand-written\n"
                    "    description: keep me\n    interval: 1d\n")
        s = _spec_state(autonomous=[{"description": "daily digest",
                                     "leash": "notify", "cadence": "1d"}])
        merged = yaml.safe_load(authoring.merge_monitors_yaml(existing, s))
        names = {m["name"] for m in merged["monitors"]}
        assert "hand-written" in names                  # kept
        assert "daily-digest" in names                  # added from spec


class TestCustomServiceTools:
    """A service Venn doesn't cover gets an authored tools guide + its own
    API-key credential in agent.yaml (the #4 'posthog.md' path)."""

    def _state_with_custom(self):
        s = _spec_state()
        # github is native; posthog is custom (empty Venn catalog forces it).
        s.spec.services = [{"name": "github"}, {"name": "posthog"}]
        return s

    def test_manifest_includes_a_guide_for_each_custom_service(self):
        s = self._state_with_custom()
        paths = [f.path for f in compute_manifest(s, catalog=set())]
        assert "tools/posthog.md" in paths
        # native services don't get a custom guide
        assert "tools/github.md" not in paths

    def test_custom_service_gets_api_key_credential_in_agent_yaml(self):
        s = self._state_with_custom()
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        ph = next(x for x in cfg["services"] if x["name"] == "posthog")
        assert ph["credentials"] == {"api_key": "${POSTHOG_API_KEY}"}

    def test_in_catalog_service_is_not_treated_as_custom(self):
        s = _spec_state()
        s.spec.services = [{"name": "zendesk"}]
        paths = [f.path for f in compute_manifest(s, catalog={"zendesk"})]
        assert not any(p.startswith("tools/") for p in paths)


class TestHostedMcpServers:
    """A service Venn doesn't cover but that ships a hosted MCP is wired into
    agent.yaml `mcp_servers:` — not the `services:` block, no authored guide."""

    def _state_with_mcp(self):
        s = _spec_state()
        # github native; stripe = static-key MCP; deepwiki = public MCP.
        s.spec.services = [{"name": "github"}, {"name": "stripe"},
                           {"name": "deepwiki"}]
        return s

    def test_static_key_mcp_emits_url_and_auth_header(self):
        cfg = yaml.safe_load(
            authoring.build_agent_yaml(self._state_with_mcp(), catalog=set()))
        mcp = cfg["mcp_servers"]
        assert mcp["stripe"]["type"] == "http"
        assert mcp["stripe"]["url"].startswith("https://")
        # key referenced as ${VAR}, never a literal
        assert mcp["stripe"]["headers"] == {
            "Authorization": "Bearer ${STRIPE_API_KEY}"}

    def test_public_mcp_emits_url_only(self):
        cfg = yaml.safe_load(
            authoring.build_agent_yaml(self._state_with_mcp(), catalog=set()))
        assert "headers" not in cfg["mcp_servers"]["deepwiki"]

    def test_mcp_services_not_in_services_block(self):
        cfg = yaml.safe_load(
            authoring.build_agent_yaml(self._state_with_mcp(), catalog=set()))
        names = {s["name"] for s in cfg.get("services", [])}
        assert "stripe" not in names and "deepwiki" not in names
        assert "github" in names            # native still listed

    def test_mcp_service_gets_no_tools_guide(self):
        paths = [f.path for f in compute_manifest(self._state_with_mcp(),
                                                  catalog=set())]
        assert not any(p.startswith("tools/") for p in paths)

    def test_no_mcp_block_without_hosted_services(self):
        cfg = yaml.safe_load(build_agent_yaml(_spec_state()))  # github + slack
        assert "mcp_servers" not in cfg

    def test_merge_unions_mcp_servers_keeping_hand_written(self):
        existing = (
            "agent: legacy\nversion: 0.1.0\nentry_point: lead\n"
            "mcp_servers:\n"
            "  homemade:\n    type: stdio\n    command: ./run-mcp\n"
            "  stripe:\n    type: http\n    url: https://custom/stripe\n")
        s = self._state_with_mcp()
        s.team_name = "legacy"
        merged = yaml.safe_load(
            authoring.merge_agent_yaml(existing, s, catalog=set()))
        mcp = merged["mcp_servers"]
        assert mcp["homemade"]["command"] == "./run-mcp"     # hand-written kept
        assert mcp["stripe"]["url"] == "https://custom/stripe"  # not clobbered
        assert "deepwiki" in mcp                              # new one added


class TestUserMcpServers:
    """User-added custom MCP connections (name + URL + auth) are authored into
    agent.yaml mcp_servers:, kept out of the services block, and get no guide."""

    def _state(self, **mcp):
        s = _spec_state()
        s.spec.services = [{"name": "github"}, {"name": "posthog"}]
        s.spec.mcp_servers = mcp
        return s

    def test_api_key_mcp_emits_bearer_header(self):
        s = self._state(posthog={"url": "https://mcp.posthog.com/mcp",
                                 "type": "http", "auth": "api_key",
                                 "secret_var": "POSTHOG_API_KEY"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        ph = cfg["mcp_servers"]["posthog"]
        assert ph["url"] == "https://mcp.posthog.com/mcp"
        assert ph["headers"] == {"Authorization": "Bearer ${POSTHOG_API_KEY}"}

    def test_public_user_mcp_emits_url_only(self):
        s = self._state(acme={"url": "https://mcp.acme.com/mcp", "type": "http",
                              "auth": "none"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        assert "headers" not in cfg["mcp_servers"]["acme"]
        assert cfg["mcp_servers"]["acme"]["url"] == "https://mcp.acme.com/mcp"

    def test_user_mcp_not_in_services_and_no_guide(self):
        # posthog is both a (guessed) custom service AND a user MCP → it's an MCP
        # now: out of the services block, and no tools/posthog.md guide.
        s = self._state(posthog={"url": "https://mcp.posthog.com/mcp",
                                 "type": "http", "auth": "none"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        assert "posthog" not in {x["name"] for x in cfg.get("services", [])}
        paths = [f.path for f in compute_manifest(s, catalog=set())]
        assert "tools/posthog.md" not in paths

    def test_stdio_mcp_emits_command_args_env(self):
        s = self._state(substack={"type": "stdio", "command": "substack-mcp",
                                  "args": ["--stdio"],
                                  "env_vars": ["SUBSTACK_API_KEY"],
                                  "auth": "stdio"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        sub = cfg["mcp_servers"]["substack"]
        assert sub["type"] == "stdio"
        assert sub["command"] == "substack-mcp"
        assert sub["args"] == ["--stdio"]
        # Env var NAMES → `${VAR}` refs, never inline secrets.
        assert sub["env"] == {"SUBSTACK_API_KEY": "${SUBSTACK_API_KEY}"}

    def test_stdio_mcp_without_args_or_env_omits_them(self):
        s = self._state(local={"type": "stdio", "command": "my-mcp"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        loc = cfg["mcp_servers"]["local"]
        assert loc == {"type": "stdio", "command": "my-mcp"}

    def test_stdio_mcp_kept_out_of_services_block(self):
        s = self._state(substack={"type": "stdio", "command": "substack-mcp"})
        cfg = yaml.safe_load(authoring.build_agent_yaml(s, catalog=set()))
        assert "substack" not in {x["name"] for x in cfg.get("services", [])}


class TestAuthorOpenModeNonLossy:
    """author_pack in open mode edits in place — it must preserve files the
    manifest never models and never blank an existing prose file."""

    def _existing_team(self, root, name="legacy"):
        src = root / "bobi" / name
        (src / "roles" / "lead").mkdir(parents=True)
        (src / "agent.yaml").write_text(
            f"agent: {name}\nversion: 0.1.0\nentry_point: lead\n")
        (src / "agent.md").write_text("# Legacy\n\nHand-written base.\n")
        (src / "roles" / "lead" / "ROLE.md").write_text(
            "# Lead\n\nDeep hand-written role with specifics A, B, C.\n")
        (src / "tools").mkdir()
        (src / "tools" / "github.md").write_text("custom tool guide\n")
        return src

    def _open_state(self, source, name="legacy"):
        s = SetupState(team_name=name, mode="open", source_dir=str(source))
        s.spec.goal = "Watch the repo."
        s.spec.roles = [{"name": "lead", "responsibility": "classify"}]
        return s

    def test_preserves_unmodeled_files_and_uses_edit_prompt(self, tmp_path):
        src = self._existing_team(tmp_path)
        s = self._open_state(src)
        prompts = []

        async def fake(*, system_prompt, user_prompt, model, cwd):
            prompts.append((system_prompt, user_prompt))
            yield "EDITED.\n"

        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=fake)))
        pack = tmp_path / "bobi" / "legacy"
        # a file outside the manifest is never touched
        assert (pack / "tools" / "github.md").read_text() == "custom tool guide\n"
        # prose files went through the EDIT path (editing prompt + original shown)
        assert any("revise ONE existing file" in sp for sp, _ in prompts)
        assert any("specifics A, B, C" in up for _, up in prompts)

    def test_blank_edit_keeps_the_original_file(self, tmp_path):
        src = self._existing_team(tmp_path)
        s = self._open_state(src)

        async def blank(*, system_prompt, user_prompt, model, cwd):
            return
            yield  # pragma: no cover

        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=blank)))
        role = (tmp_path / "bobi" / "legacy"
                / "roles" / "lead" / "ROLE.md").read_text()
        assert "specifics A, B, C" in role  # original survived an empty edit


class TestRoleDimensionsAndAutomations:
    """The four per-role interview dimensions and the role/command on
    automations thread through authoring into ROLE.md prompts and monitors."""

    def test_normalized_roles_carries_four_dimensions(self):
        s = SetupState(team_name="t")
        s.spec.goal = "Do the thing."
        s.spec.roles = [{"name": "Triage Lead", "responsibility": "classify",
                         "good_looks_like": "fast accurate triage",
                         "systems": ["github", "slack"],
                         "triggers": "on new issue"}]
        r = authoring.normalized_roles(s)[0]
        assert r["good_looks_like"] == "fast accurate triage"
        assert r["systems"] == ["github", "slack"]
        assert r["triggers"] == "on new issue"

    def test_normalized_roles_coerces_non_string_systems(self):
        # The brain can emit non-string / null systems entries; the downstream
        # ", ".join must never crash the build pour.
        s = SetupState(team_name="t")
        s.spec.goal = "g"
        s.spec.roles = [{"name": "lead", "responsibility": "x",
                         "systems": [1, "github", None, ""]}]
        r = authoring.normalized_roles(s)[0]
        assert r["systems"] == ["1", "github"]
        assert "github" in authoring._spec_brief(s)   # renders, no TypeError

    def test_normalized_roles_defaults_for_goal_only_team(self):
        s = SetupState(team_name="t")
        s.spec.goal = "Carry the load."
        r = authoring.normalized_roles(s)[0]           # synthesized assistant
        assert r["name"] == "assistant"
        assert r["systems"] == [] and r["good_looks_like"] == ""
        assert r["triggers"] == ""

    def test_role_md_prompt_includes_the_dimensions(self):
        s = SetupState(team_name="t")
        s.spec.goal = "g"
        role = {"name": "lead", "responsibility": "classify",
                "good_looks_like": "fast triage", "systems": ["github"],
                "triggers": "on new issue"}
        p = authoring.role_md_prompt(s, role)
        assert "What a good job looks like: fast triage" in p
        assert "Systems it accesses: github" in p
        assert "What triggers it: on new issue" in p

    def test_build_monitors_yaml_folds_role_and_command(self):
        s = SetupState(team_name="t")
        s.spec.goal = "g"
        s.spec.autonomous = [{"description": "daily digest", "leash": "notify",
                              "cadence": "1d", "role": "lead",
                              "command": "summarize new issues"}]
        mon = yaml.safe_load(build_monitors_yaml(s))["monitors"][0]
        assert "Run by the lead role." in mon["description"]
        assert "Do: summarize new issues" in mon["description"]
        assert mon["notify"] is True


class TestWorkflowAuthoring:
    def test_build_workflow_yaml_parses_and_gates(self, tmp_path):
        s = _spec_state(workflows=[{
            "name": "Issue Lifecycle", "description": "triage to PR",
            "trigger": "a new issue lands",
            "steps": [
                {"name": "triage", "role": "Triage Lead",
                 "prompt": "Classify the issue."},
                {"name": "plan", "role": "router",
                 "prompt": "Propose a fix plan.", "hitl": True},
                {"name": "done", "role": "nobody-known", "prompt": "Wrap up."},
            ]}])
        text = authoring.build_workflow_yaml(s, s.spec.workflows[0])
        path = tmp_path / "issue-lifecycle.yaml"
        path.write_text(text)
        from bobi.workflow.schema import load_workflow
        wf = load_workflow(path)
        assert wf.name == "issue-lifecycle"
        assert wf.trigger == "a new issue lands"
        assert [st.name for st in wf.steps] == [
            "triage", "plan", "plan-approval", "done"]
        assert wf.step_by_name("plan-approval").await_event == "approval"
        assert wf.step_by_name("triage").agent == "triage-lead"
        # a role the team doesn't have is dropped, not authored broken
        assert wf.step_by_name("done").agent == ""

    def test_manifest_includes_spec_workflows(self):
        s = _spec_state(workflows=[
            {"name": "release", "trigger": "cut a release",
             "steps": [{"name": "ship", "prompt": "Do it."}]},
            {"name": "adhoc", "steps": []},   # can't shadow the stub
            {"steps": []},                    # unnamed → skipped
        ])
        paths_ = [f.path for f in compute_manifest(s)]
        assert "workflows/release.yaml" in paths_
        assert paths_.count("workflows/adhoc.yaml") == 1

    def test_empty_steps_fall_back_to_single_step(self):
        s = _spec_state(workflows=[{"name": "x", "description": "do x",
                                    "steps": []}])
        wf = yaml.safe_load(authoring.build_workflow_yaml(s, s.spec.workflows[0]))
        assert wf["steps"] == [{"name": "run", "prompt": "do x"}]


class TestEventAutomations:
    def _auto(self, **kw):
        base = {"description": "React to incoming email", "leash": "act",
                "trigger": "event", "cadence": "when an email arrives",
                "role": "Triage Lead", "command": "Summarize the email."}
        base.update(kw)
        return base

    def test_event_automation_becomes_workflow_not_monitor(self):
        s = _spec_state(autonomous=[self._auto()])
        paths_ = [f.path for f in compute_manifest(s)]
        assert "workflows/auto-react-to-incoming-email.yaml" in paths_
        assert not any(p.startswith("monitors/") for p in paths_)

    def test_schedule_automation_still_becomes_monitor(self):
        s = _spec_state(autonomous=[{"description": "daily digest",
                                     "leash": "notify", "trigger": "schedule",
                                     "cadence": "1d"}])
        paths_ = [f.path for f in compute_manifest(s)]
        assert "monitors/defaults.yaml" in paths_
        assert not any(p.startswith("workflows/auto-") for p in paths_)

    def test_legacy_automation_without_trigger_stays_a_monitor(self):
        s = _spec_state(autonomous=[{"description": "old behavior",
                                     "cadence": "15m"}])
        mons = yaml.safe_load(build_monitors_yaml(s))["monitors"]
        assert mons[0]["name"] == "old-behavior"

    def test_event_automations_are_excluded_from_monitors(self):
        s = _spec_state(autonomous=[self._auto(),
                                    {"description": "digest", "cadence": "1d"}])
        mons = yaml.safe_load(build_monitors_yaml(s))["monitors"]
        assert [m["name"] for m in mons] == ["digest"]

    def test_ask_leash_gets_approval_gate(self, tmp_path):
        s = _spec_state(autonomous=[self._auto(leash="ask")])
        text = authoring.build_automation_workflow_yaml(s, s.spec.autonomous[0])
        path = tmp_path / "wf.yaml"
        path.write_text(text)
        from bobi.workflow.schema import load_workflow
        wf = load_workflow(path)
        assert [st.name for st in wf.steps] == ["react", "react-approval", "act"]
        assert wf.steps[1].await_event == "approval"
        assert wf.trigger == "when an email arrives"
        assert wf.steps[0].agent == "triage-lead"

    def test_event_only_automations_write_no_monitors_file(self):
        s = _spec_state(autonomous=[self._auto()])
        assert not authoring.has_monitors(s)


class TestSlackChannelsKnob:
    def test_slack_chat_service_carries_channels_ref(self):
        s = _spec_state()
        s.chat = "slack"
        cfg = yaml.safe_load(build_agent_yaml(s))
        slack = next(r for r in cfg["services"] if r["name"] == "slack")
        assert slack["channels"] == "${SLACK_CHANNELS:-}"   # optional: channel is saved AFTER deploy

    def test_non_slack_chat_has_no_channels_knob(self):
        cfg = yaml.safe_load(build_agent_yaml(_spec_state()))
        slack = next(r for r in cfg["services"] if r["name"] == "slack")
        assert "channels" not in slack


class TestWorkflowCollisions:
    def _wf(self, name):
        return {"name": name, "steps": [{"name": "a", "prompt": "p"}]}

    def _auto(self):
        return {"description": "React to email", "trigger": "event",
                "leash": "act", "cadence": "when an email arrives"}

    def test_colliding_names_get_suffixes_not_dropped(self):
        s = _spec_state(
            workflows=[self._wf("adhoc"), self._wf("My Flow"),
                       self._wf("my flow")],
            autonomous=[self._auto(), self._auto()])
        paths_ = [f.path for f in compute_manifest(s)]
        assert paths_.count("workflows/adhoc.yaml") == 1   # the stub wins
        assert "workflows/adhoc-2.yaml" in paths_
        assert "workflows/my-flow.yaml" in paths_
        assert "workflows/my-flow-2.yaml" in paths_
        assert "workflows/auto-react-to-email.yaml" in paths_
        assert "workflows/auto-react-to-email-2.yaml" in paths_

    def test_suffixed_file_carries_suffixed_name(self):
        s = _spec_state(workflows=[self._wf("adhoc")])
        spec = next(f for f in compute_manifest(s)
                    if f.path == "workflows/adhoc-2.yaml")
        assert yaml.safe_load(spec.content)["name"] == "adhoc-2"


class TestAutomationLeashBranches:
    def _auto(self, leash):
        return {"description": "React to email", "trigger": "event",
                "leash": leash, "cadence": "when an email arrives",
                "command": "Summarize it."}

    def test_notify_leash_observes_only(self):
        s = _spec_state(autonomous=[self._auto("notify")])
        wf = yaml.safe_load(
            authoring.build_automation_workflow_yaml(s, s.spec.autonomous[0]))
        assert len(wf["steps"]) == 1
        assert "do not take action yourself" in wf["steps"][0]["prompt"]

    def test_act_leash_acts_and_reports(self):
        s = _spec_state(autonomous=[self._auto("act")])
        wf = yaml.safe_load(
            authoring.build_automation_workflow_yaml(s, s.spec.autonomous[0]))
        assert len(wf["steps"]) == 1
        assert "Do it, then report" in wf["steps"][0]["prompt"]


class TestEventOnlyValidation:
    def _pack(self, tmp_path, s, with_monitors_file=True):
        from bobi.setup.actions import validate_pack
        pack = tmp_path / "pack"
        (pack / "roles" / "triage-lead").mkdir(parents=True)
        (pack / "agent.yaml").write_text(build_agent_yaml(s))
        (pack / "agent.md").write_text("# team\n")
        (pack / "roles/triage-lead/ROLE.md").write_text("# role\n")
        for fs in compute_manifest(s):
            if fs.path.startswith("workflows/") or (
                    with_monitors_file and fs.path.startswith("monitors/")):
                target = pack / fs.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fs.content)
        project = tmp_path / "run"
        project.mkdir()
        return validate_pack(pack, s, project)

    def test_event_only_automations_pass_validation(self, tmp_path):
        # Event automations ship as workflows — validation must not demand a
        # monitors file that build (correctly) never writes.
        s = _spec_state(autonomous=[{"description": "React to email",
                                     "trigger": "event", "leash": "act"}])
        findings = self._pack(tmp_path, s)
        assert not [d for ok, d in findings if not ok]

    def test_scheduled_without_monitors_file_still_fails(self, tmp_path):
        s = _spec_state(autonomous=[{"description": "daily digest",
                                     "cadence": "1d", "leash": "notify"}])
        findings = self._pack(tmp_path, s, with_monitors_file=False)
        bad = [d for ok, d in findings if not ok]
        assert any("monitors/defaults.yaml is missing" in d for d in bad)


class TestOpenModeWorkflowFiles:
    def test_spec_workflows_overwrite_but_adhoc_keeps_hand_edits(self, tmp_path):
        from bobi.setup.authoring import _deterministic_body
        s = _spec_state(workflows=[{"name": "flow",
                                    "steps": [{"name": "a", "prompt": "new"}]}])
        manifest = compute_manifest(s)
        flow = next(f for f in manifest if f.path == "workflows/flow.yaml")
        target = tmp_path / "flow.yaml"
        target.write_text("name: flow\nsteps: []\n")
        assert "new" in _deterministic_body(flow, target, s)
        adhoc = next(f for f in manifest if f.path == "workflows/adhoc.yaml")
        t2 = tmp_path / "adhoc.yaml"
        t2.write_text("hand-edited\n")
        assert _deterministic_body(adhoc, t2, s) == "hand-edited\n"

    def test_merge_monitors_drops_migrated_event_behavior(self):
        # schedule → event flip must not leave the old poll monitor running
        # alongside the new event workflow.
        s = _spec_state(autonomous=[{"description": "React to email",
                                     "trigger": "event", "leash": "act"}])
        existing = yaml.dump({"monitors": [
            {"name": "react-to-email", "interval": "15m"},
            {"name": "hand-written", "interval": "1h"}]})
        merged = yaml.safe_load(
            authoring.merge_monitors_yaml(existing, s))["monitors"]
        assert [m["name"] for m in merged] == ["hand-written"]


class TestWorkflowAuthoringEdges:
    def test_step_name_fallbacks_dedupe_and_prompt_fallback(self):
        s = _spec_state(workflows=[{"name": "edgy", "steps": [
            {"name": "same", "prompt": "one"},
            {"name": "same", "prompt": "two"},
            {"prompt": "unnamed gets step-N"},          # no name → step-3
            {"name": "descy", "description": "from description"},
            {"name": "empty"},                           # no prompt → skipped
        ]}])
        wf = yaml.safe_load(authoring.build_workflow_yaml(s, s.spec.workflows[0]))
        names = [st["name"] for st in wf["steps"]]
        assert names == ["same", "same-2", "step-3", "descy"]
        assert wf["steps"][3]["prompt"] == "from description"

    def test_workflow_slug_and_trigger_fallbacks(self):
        s = _spec_state()
        assert authoring.workflow_slug({"name": "!!"}) == "workflow"
        wf = yaml.safe_load(authoring.build_workflow_yaml(
            s, {"name": "quiet", "description": "does a thing",
                "steps": [{"name": "a", "prompt": "p"}]}))
        assert wf["trigger"] == "does a thing"     # trigger ← description
        wf2 = yaml.safe_load(authoring.build_workflow_yaml(
            s, {"name": "bare", "steps": [{"name": "a", "prompt": "p"}]}))
        assert wf2["trigger"] == "bare"            # trigger ← name

    def test_automation_name_truncation_and_fallbacks(self):
        long_desc = "watch " + "x" * 60
        assert authoring.automation_workflow_name(
            {"description": long_desc}).startswith("auto-watch-")
        assert len(authoring.automation_workflow_name(
            {"description": long_desc})) <= 45
        assert authoring.automation_workflow_name({}) == "auto-behavior"
        # An event automation with a blank description ships nothing (matches
        # the monitors path) — and never crashes the manifest.
        s = _spec_state(autonomous=[{"description": "  ", "trigger": "event"}])
        assert not any(p.path.startswith("workflows/auto-")
                       for p in compute_manifest(s))

    def test_spec_brief_names_workflows(self):
        s = _spec_state(workflows=[{"name": "flow", "trigger": "a PR opens",
                                    "steps": []}])
        brief = authoring._spec_brief(s)
        assert "flow (on: a PR opens)" in brief
        assert "Workflows: none" in authoring._spec_brief(_spec_state())
