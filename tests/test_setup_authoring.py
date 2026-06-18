"""Tests for the Build authoring step — deterministic structure, the
manifest, and a full pour that produces a pack passing validation."""

import asyncio

import pytest
import yaml

from modastack.setup import actions, authoring
from modastack.setup.authoring import (
    build_adhoc_yaml,
    build_agent_yaml,
    build_monitors_yaml,
    cadence_to_interval,
    compute_entry_point,
    compute_manifest,
    slug,
)
from modastack.setup.state import SetupState


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
        names = {s["name"] for s in cfg["services"]}
        assert names == {"github", "slack"}
        slack = next(s for s in cfg["services"] if s["name"] == "slack")
        assert slack["credentials"]["bot_token"] == "${SLACK_BOT_TOKEN}"
        assert slack["events"] is True

    def test_venn_services_declare_the_shared_key(self):
        # A team using Venn-backed services must declare venn_api_key so
        # `modastack start` resolves it from the env / .env (else preflight
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
        from modastack.workflow.schema import load_workflow
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
        from modastack.monitors.schema import Monitor, parse_interval
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

        pack = tmp_path / "agents" / "triage-bot"
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

    def test_create_lands_in_named_subfolder_under_the_base(self, tmp_path):
        # Create's location is a BASE; the team lands at <base>/<name> so every
        # team gets its own folder (no collision between two creates).
        s = _spec_state()                 # team_name="triage-bot", mode=create
        s.source_dir = "modastack"            # the base the user chose
        _run(_collect(authoring.author_pack(
            s, tmp_path, stream_fn=self._fake_stream())))
        assert (tmp_path / "modastack" / "triage-bot" / "agent.yaml").is_file()
        # the concrete path is persisted (idempotent — not re-appended)
        assert s.source_dir == "modastack/triage-bot"
        s.team_name = "triage-bot"
        assert actions.team_source_dir(tmp_path, s) == tmp_path / "modastack" / "triage-bot"

    def test_create_refuses_to_overwrite_an_existing_library_team(self, tmp_path):
        # Two creates auto-named the same slug must not clobber each other —
        # the second blocks instead of silently overwriting the first's source.
        existing = tmp_path / "modastack" / "triage-bot"
        existing.mkdir(parents=True)
        (existing / "agent.yaml").write_text("agent: the-original\n")
        s = _spec_state()                 # team_name="triage-bot", mode=create
        s.source_dir = "modastack"        # the base — not yet claimed
        with pytest.raises(actions.ActionError, match="already exists"):
            _run(_collect(authoring.author_pack(
                s, tmp_path, stream_fn=self._fake_stream())))
        # the original team's source is untouched
        assert (existing / "agent.yaml").read_text() == "agent: the-original\n"

    def test_create_reauthors_its_own_claimed_folder(self, tmp_path):
        # Re-running build once the team is claimed (source_dir already points at
        # <base>/<slug>) is fine — not a collision with a different team.
        existing = tmp_path / "modastack" / "triage-bot"
        existing.mkdir(parents=True)
        (existing / "agent.yaml").write_text("agent: triage-bot\n")
        s = _spec_state()
        s.source_dir = "modastack/triage-bot"   # already ours
        _run(_collect(authoring.author_pack(
            s, tmp_path, stream_fn=self._fake_stream())))
        assert (existing / "agent.yaml").is_file()   # re-authored, no error

    def test_pour_failure_preserves_original_file_in_open_mode(self, tmp_path):
        # Regression (review F3): a mid-pour failure (llm.stream raises — e.g. the
        # model stalls past the idle timeout) must NOT truncate the user's
        # existing file. In open/modify mode the original agent.md/ROLE.md must
        # survive an authoring failure intact, never be left half-written.
        from modastack.setup import open_mode
        from modastack.setup.llm import LLMError

        src = tmp_path / "modastack-agents" / "myteam"
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
        agent_md = (tmp_path / "agents" / "t" / "agent.md").read_text()
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
        agent_md = (tmp_path / "agents" / "t" / "agent.md").read_text()
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


class TestAuthorOpenModeNonLossy:
    """author_pack in open mode edits in place — it must preserve files the
    manifest never models and never blank an existing prose file."""

    def _existing_team(self, root, name="legacy"):
        src = root / "modastack" / name
        (src / "roles" / "lead").mkdir(parents=True)
        (src / "agent.yaml").write_text(
            f"agent: {name}\nversion: 0.1.0\nentry_point: lead\n")
        (src / "agent.md").write_text("# Legacy\n\nHand-written base.\n")
        (src / "roles" / "lead" / "ROLE.md").write_text(
            "# Lead\n\nDeep hand-written role with specifics A, B, C.\n")
        (src / "tools").mkdir()
        (src / "tools" / "github.md").write_text("custom tool guide\n")
        return src

    def _open_state(self, name="legacy"):
        s = SetupState(team_name=name, mode="open", source_dir=f"modastack/{name}")
        s.spec.goal = "Watch the repo."
        s.spec.roles = [{"name": "lead", "responsibility": "classify"}]
        return s

    def test_preserves_unmodeled_files_and_uses_edit_prompt(self, tmp_path):
        self._existing_team(tmp_path)
        s = self._open_state()
        prompts = []

        async def fake(*, system_prompt, user_prompt, model, cwd):
            prompts.append((system_prompt, user_prompt))
            yield "EDITED.\n"

        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=fake)))
        pack = tmp_path / "modastack" / "legacy"
        # a file outside the manifest is never touched
        assert (pack / "tools" / "github.md").read_text() == "custom tool guide\n"
        # prose files went through the EDIT path (editing prompt + original shown)
        assert any("revise ONE existing file" in sp for sp, _ in prompts)
        assert any("specifics A, B, C" in up for _, up in prompts)

    def test_blank_edit_keeps_the_original_file(self, tmp_path):
        self._existing_team(tmp_path)
        s = self._open_state()

        async def blank(*, system_prompt, user_prompt, model, cwd):
            return
            yield  # pragma: no cover

        _run(_collect(authoring.author_pack(s, tmp_path, stream_fn=blank)))
        role = (tmp_path / "modastack" / "legacy"
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
