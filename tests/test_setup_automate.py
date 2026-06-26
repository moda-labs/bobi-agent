"""Tests for the Automate suggester — prompt input + tolerant JSON parsing."""

import asyncio
import json

from bobi.setup import automate
from bobi.setup.automate import _parse_suggestions
from bobi.setup.state import SetupState


def _run(coro):
    return asyncio.run(coro)


class TestParseSuggestions:
    def test_normalizes_valid_array(self):
        text = json.dumps([
            {"description": "Flag stale PRs", "leash": "notify",
             "cadence": "1d", "rationale": "keeps reviews moving"},
            {"description": "Daily digest", "leash": "act", "cadence": "9am"}])
        out = _parse_suggestions(text)
        assert len(out) == 2
        assert out[0]["description"] == "Flag stale PRs"
        assert out[1]["rationale"] == ""

    def test_bad_leash_defaults_to_notify(self):
        out = _parse_suggestions('[{"description": "x", "leash": "yolo"}]')
        assert out[0]["leash"] == "notify"

    def test_drops_descriptionless_items(self):
        out = _parse_suggestions('[{"leash": "act"}, {"description": "ok"}]')
        assert [o["description"] for o in out] == ["ok"]

    def test_tolerates_prose_around_array(self):
        out = _parse_suggestions('Here you go:\n[{"description": "a"}]\ndone')
        assert out[0]["description"] == "a"

    def test_garbage_is_empty(self):
        assert _parse_suggestions("no json here") == []
        assert _parse_suggestions("[not json") == []

    def test_empty_array_is_no_suggestions(self):
        assert _parse_suggestions("[]") == []


class TestSuggest:
    def test_uses_injected_source(self):
        async def fake(*, system_prompt, user_prompt, model, cwd):
            assert "Goal:" in user_prompt
            yield json.dumps([{"description": "Watch deploys", "leash": "ask",
                               "cadence": "on deploy"}])

        s = SetupState()
        s.spec.goal = "Ship and watch deploys."
        out = _run(automate.suggest(s, stream_fn=fake))
        assert out == [{"description": "Watch deploys", "leash": "ask",
                        "cadence": "on deploy", "rationale": ""}]
