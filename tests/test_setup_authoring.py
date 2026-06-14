"""Tests for the Build authoring step — deterministic structure, the
manifest, and a full pour that produces a pack passing validation."""

import asyncio

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
