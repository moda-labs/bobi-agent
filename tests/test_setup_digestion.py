"""Tests for the digestion brain — context assembly, output parsing, delta
routing, the streaming reply splitter, and a full hermetic turn."""

import asyncio
import json

from bobi.setup import digestion
from bobi.setup.digestion import (
    SPEC_SENTINEL,
    DigestionResult,
    _ReplySplitter,
    apply_deltas,
    assemble_context,
    parse_digestion,
)
from bobi.setup.state import Readiness, SetupState


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [c async for c in agen]


def _payload(**kw):
    return SPEC_SENTINEL + "\n" + json.dumps(kw)


class TestAssembleContext:
    def test_includes_spec_summary_and_recent_messages(self):
        s = SetupState(summary="building a triage bot")
        s.spec.goal = "Triage issues."
        s.messages = [{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "hey"}]
        ctx = assemble_context(s)
        assert "Triage issues." in ctx
        assert "building a triage bot" in ctx
        assert "hi" in ctx and "hey" in ctx

    def test_caps_to_last_n_messages(self):
        s = SetupState()
        s.messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        ctx = assemble_context(s, last_n=3)
        assert "m19" in ctx and "m17" in ctx
        assert "m16" not in ctx


class TestParseDigestion:
    def test_splits_reply_and_payload(self):
        text = ("Got it — a triage bot.\n" + _payload(
            deltas={"goal": "Triage issues."},
            summary="triage bot", readiness={"goal": "enough"}))
        r = parse_digestion(text)
        assert r.reply == "Got it — a triage bot."
        assert r.deltas == {"goal": "Triage issues."}
        assert r.summary == "triage bot"
        assert r.readiness == {"goal": "enough"}

    def test_no_sentinel_is_all_reply(self):
        r = parse_digestion("just chatting, no spec block")
        assert r.reply == "just chatting, no spec block"
        assert r.deltas == {}

    def test_malformed_json_degrades_to_reply_only(self):
        r = parse_digestion("hello\n" + SPEC_SENTINEL + "\n{not json")
        assert r.reply == "hello"
        assert r.deltas == {}

    def test_tolerates_trailing_prose_after_json(self):
        r = parse_digestion("hi\n" + SPEC_SENTINEL + '\n{"summary": "s"}\nbye')
        assert r.summary == "s"

    def test_parses_phase(self):
        text = "ok\n" + _payload(phase="role:lead", summary="s")
        assert parse_digestion(text).phase == "role:lead"


class TestApplyDeltas:
    def test_routes_each_slot_as_full_replacement(self):
        s = SetupState()
        r = DigestionResult(
            reply="ok",
            deltas={"goal": "  Do the thing.  ",
                    "roles": [{"name": "lead", "responsibility": "drives"}],
                    "services": [{"name": "github"}],
                    "autonomous": [{"description": "daily digest",
                                    "leash": "notify", "cadence": "1d"}]},
            autonomous_confirmed=True,
            summary="rolling summary",
            readiness={"goal": "enough", "bogus": "enough", "roles": "nonsense"})
        apply_deltas(s, r)
        assert s.spec.goal == "Do the thing."
        assert s.spec.roles[0]["name"] == "lead"
        assert s.spec.services == [{"name": "github"}]
        assert s.spec.autonomous[0]["description"] == "daily digest"
        assert s.spec.autonomous_confirmed is True
        assert s.summary == "rolling summary"
        # valid readiness kept; unknown slot + bad value dropped
        assert s.spec.readiness == {"goal": "enough"}
        assert s.spec.readiness_for("goal") == Readiness.ENOUGH

    def test_absent_slots_are_left_untouched(self):
        s = SetupState()
        s.spec.goal = "keep me"
        apply_deltas(s, DigestionResult(reply="", deltas={"roles": []}))
        assert s.spec.goal == "keep me"

    def test_empty_summary_does_not_clobber(self):
        s = SetupState(summary="existing")
        apply_deltas(s, DigestionResult(reply="", summary=""))
        assert s.summary == "existing"

    def test_auto_names_a_new_team_from_the_first_name_delta(self):
        s = SetupState(mode="create")          # unnamed create
        apply_deltas(s, DigestionResult(reply="", deltas={"name": "GitHub Triage"}))
        assert s.team_name == "github-triage"  # slugged

    def test_auto_name_is_set_once_and_then_stable(self):
        s = SetupState(mode="create", team_name="github-triage")
        apply_deltas(s, DigestionResult(reply="", deltas={"name": "Something Else"}))
        assert s.team_name == "github-triage"  # not overwritten once named

    def test_open_mode_never_auto_renames(self):
        s = SetupState(mode="open", team_name="legacy-bot")
        apply_deltas(s, DigestionResult(reply="", deltas={"name": "New Name"}))
        assert s.team_name == "legacy-bot"     # existing team keeps its name

    def test_phase_is_routed_into_state(self):
        s = SetupState()
        apply_deltas(s, DigestionResult(reply="", phase="role:lead"))
        assert s.phase == "role:lead"

    def test_empty_phase_does_not_clobber(self):
        s = SetupState(phase="automations")
        apply_deltas(s, DigestionResult(reply="", phase=""))
        assert s.phase == "automations"

    def test_role_dimensions_and_status_round_trip(self):
        s = SetupState()
        role = {"name": "lead", "responsibility": "classify",
                "good_looks_like": "fast, accurate triage",
                "systems": ["github"], "triggers": "on new issue",
                "status": "complete"}
        apply_deltas(s, DigestionResult(reply="", deltas={"roles": [role]}))
        assert s.spec.roles[0]["status"] == "complete"
        assert s.spec.roles[0]["systems"] == ["github"]


class TestReplySplitter:
    def test_emits_reply_before_sentinel_only(self):
        sp = _ReplySplitter(SPEC_SENTINEL)
        out = "".join(sp.feed(c) for c in ["Hel", "lo", " there"])
        out += sp.flush()
        # nothing held back forever when no sentinel arrives
        assert "".join([out]) == "Hello there"

    def test_holds_back_split_sentinel(self):
        sp = _ReplySplitter(SPEC_SENTINEL)
        chunks = ["reply text", "\n===BO", "BI-SPEC===\n", '{"summary":"s"}']
        emitted = "".join(sp.feed(c) for c in chunks) + sp.flush()
        assert emitted == "reply text\n"
        assert sp.text.endswith('{"summary":"s"}')

    def test_sentinel_in_single_chunk(self):
        sp = _ReplySplitter(SPEC_SENTINEL)
        emitted = sp.feed("hi " + SPEC_SENTINEL + " junk") + sp.flush()
        assert emitted == "hi "


class TestDigestTurn:
    def test_streams_reply_and_routes_payload(self, tmp_path):
        chunks = ["A triage ", "bot it is.\n",
                  _payload(deltas={"goal": "Triage incoming issues."},
                           summary="triage bot",
                           readiness={"goal": "enough"})]

        async def fake(*, system_prompt, user_prompt, model, cwd):
            for c in chunks:
                yield c

        s = SetupState(team_name="t")
        streamed = _run(_collect(
            digestion.digest_turn(s, tmp_path, "build me a triage bot",
                                  stream_fn=fake)))
        # the reply (pre-sentinel) streams to the UI; trailing whitespace ok
        assert "".join(streamed).strip() == "A triage bot it is."
        # routed into authoritative state + persisted
        assert s.spec.goal == "Triage incoming issues."
        assert s.summary == "triage bot"
        assert s.messages[0] == {"role": "user",
                                 "content": "build me a triage bot"}
        assert s.messages[1]["role"] == "assistant"
        assert SetupState.load(tmp_path).spec.goal == "Triage incoming issues."

    def test_redacts_secret_before_llm_and_transcript(self, tmp_path):
        # built from parts so the source has no contiguous token literal
        secret = "xoxb-" + "9" * 12 + "-" + "a" * 16
        seen = {}

        async def fake(*, system_prompt, user_prompt, model, cwd):
            seen["prompt"] = user_prompt
            yield "noted."

        s = SetupState(team_name="t")
        _run(_collect(digestion.digest_turn(
            s, tmp_path, f"my slack token is {secret}", stream_fn=fake)))
        # never reaches the LLM, never persisted to the transcript
        assert secret not in seen["prompt"]
        assert secret not in s.messages[0]["content"]
        assert "[redacted]" in s.messages[0]["content"]
        assert secret not in SetupState.load(tmp_path).messages[0]["content"]

    def test_malformed_turn_still_records_reply(self, tmp_path):
        async def fake(*, system_prompt, user_prompt, model, cwd):
            yield "just a chat reply, no block"

        s = SetupState(team_name="t")
        _run(_collect(digestion.digest_turn(s, tmp_path, "hi", stream_fn=fake)))
        assert s.messages[-1]["content"] == "just a chat reply, no block"
        assert s.spec.goal == ""
