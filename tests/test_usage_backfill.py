"""Tests for the historical session-usage backfill (#935).

Sessions recorded before the camel-case normalization fix carry provider
dollars with every token counter at zero, or nothing at all. The backfill
recovers token telemetry from Claude's retained JSONL transcripts and fills
ONLY what is missing, never overwriting a recorded fact.
"""

import json

import pytest

from bobi.sdk import SessionEntry, SessionRegistry
from bobi.usage_backfill import backfill_usage, transcript_usage


ASSISTANT_MODEL = "claude-opus-4-8"


def _assistant(message_id, model=ASSISTANT_MODEL, *, inp=2, out=10,
               cache_read=15_282, cache_creation=24_576, sidechain=False,
               session_id="sess-1"):
    """One assistant line in Claude's JSONL shape."""
    return {
        "type": "assistant",
        "uuid": f"uuid-{message_id}",
        "requestId": f"req-{message_id}",
        "sessionId": session_id,
        "isSidechain": sidechain,
        "cwd": "/work",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _write_transcript(claude_dir, session_id, lines, project="-work"):
    d = claude_dir / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(line) + "\n" if isinstance(line, dict) else line
                for line in lines)
    )
    return path


@pytest.fixture
def claude_dir(tmp_path):
    d = tmp_path / "claude"
    (d / "projects").mkdir(parents=True)
    return d


@pytest.fixture
def registry(bobi_install):
    return SessionRegistry()


def _register(registry, name, session_id, **kw):
    registry.register(SessionEntry(
        name=name, session_id=session_id, status="completed",
        cwd="/work", **kw,
    ))
    return name


def _state(registry, name):
    return json.loads((registry.session_dir(name) / "state.json").read_text())


# --- transcript parsing ----------------------------------------------------


class TestTranscriptUsage:
    def test_sums_assistant_usage_grouped_by_model(self, claude_dir):
        path = _write_transcript(claude_dir, "s1", [
            _assistant("msg-a"),
            _assistant("msg-b", model="claude-haiku-4-5", inp=1, out=2,
                       cache_read=3, cache_creation=4),
        ])
        usage = transcript_usage(path)
        assert usage[ASSISTANT_MODEL] == {
            "input_tokens": 39_860,      # 2 + 24,576 + 15,282
            "cached_input_tokens": 15_282,
            "output_tokens": 10,
        }
        assert usage["claude-haiku-4-5"] == {
            "input_tokens": 8, "cached_input_tokens": 3, "output_tokens": 2,
        }

    def test_repeated_message_id_counted_once(self, claude_dir):
        """Claude writes one assistant line per content block, all carrying the
        same message id and the same usage. Summing lines overcounts a real
        transcript by ~2x, so usage is deduped by message id."""
        path = _write_transcript(claude_dir, "s1", [
            _assistant("msg-a"), _assistant("msg-a"), _assistant("msg-a"),
        ])
        assert transcript_usage(path)[ASSISTANT_MODEL]["output_tokens"] == 10

    def test_subagent_usage_is_counted(self, claude_dir):
        """Task-tool sidechains are real spend on the same session."""
        path = _write_transcript(claude_dir, "s1", [
            _assistant("msg-a"),
            _assistant("msg-b", sidechain=True, inp=5, out=7,
                       cache_read=0, cache_creation=0),
        ])
        assert transcript_usage(path)[ASSISTANT_MODEL]["output_tokens"] == 17

    def test_malformed_lines_are_skipped(self, claude_dir):
        path = _write_transcript(claude_dir, "s1", [
            "{not json\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({"type": "assistant"}) + "\n",
            _assistant("msg-a"),
        ])
        assert transcript_usage(path)[ASSISTANT_MODEL]["output_tokens"] == 10

    def test_synthetic_model_with_no_usage_is_dropped(self, claude_dir):
        """Claude books interrupts and API-error placeholders under the
        pseudo-model `<synthetic>` with no tokens. Recording it would create a
        permanently zero entry that every later pass re-repairs."""
        path = _write_transcript(claude_dir, "s1", [
            _assistant("msg-a", model="<synthetic>", inp=0, out=0,
                       cache_read=0, cache_creation=0),
        ])
        assert transcript_usage(path) == {}

    def test_unreadable_transcript_raises(self, claude_dir):
        with pytest.raises(OSError):
            transcript_usage(claude_dir / "projects" / "nope.jsonl")


# --- backfill --------------------------------------------------------------


class TestBackfill:
    def test_dry_run_makes_no_filesystem_changes(self, registry, claude_dir):
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])
        before = (registry.session_dir("s") / "state.json").read_text()

        report = backfill_usage(claude_config_dir=claude_dir, write=False)

        assert report.repaired == 1
        assert (registry.session_dir("s") / "state.json").read_text() == before

    def test_write_fills_zero_token_usage(self, registry, claude_dir):
        _register(registry, "s", "sess-1", total_cost_usd=0.253661,
                  model=ASSISTANT_MODEL, provider="anthropic",
                  model_usage={f"anthropic:{ASSISTANT_MODEL}": {
                      "cost_usd": 0.253661, "input_tokens": 0,
                      "output_tokens": 0, "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.repaired == 1
        entry = _state(registry, "s")["model_usage"][
            f"anthropic:{ASSISTANT_MODEL}"]
        assert entry["input_tokens"] == 39_860
        assert entry["cached_input_tokens"] == 15_282
        assert entry["output_tokens"] == 10
        # Provider-reported dollars survive untouched.
        assert entry["cost_usd"] == 0.253661
        assert _state(registry, "s")["total_cost_usd"] == 0.253661

    def test_write_fills_empty_model_usage(self, registry, claude_dir):
        """Older sessions recorded no model_usage at all."""
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        state = _state(registry, "s")
        assert state["model_usage"][f"anthropic:{ASSISTANT_MODEL}"][
            "output_tokens"] == 10
        assert state["model"] == ASSISTANT_MODEL
        assert state["provider"] == "anthropic"
        # No provider cost is recoverable from a transcript; it stays zero.
        assert state["total_cost_usd"] == 0.0

    def test_existing_usage_is_never_overwritten(self, registry, claude_dir):
        _register(registry, "s", "sess-1",
                  model_usage={f"anthropic:{ASSISTANT_MODEL}": {
                      "cost_usd": 1.0, "input_tokens": 7,
                      "output_tokens": 3, "cached_input_tokens": 1}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.already_populated == 1
        assert report.repaired == 0
        assert _state(registry, "s")["model_usage"][
            f"anthropic:{ASSISTANT_MODEL}"]["input_tokens"] == 7

    def test_applying_twice_is_idempotent(self, registry, claude_dir):
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        backfill_usage(claude_config_dir=claude_dir, write=True)
        first = _state(registry, "s")
        second_report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert second_report.repaired == 0
        assert second_report.already_populated == 1
        assert _state(registry, "s") == first

    def test_transcript_with_only_synthetic_turns_is_never_re_repaired(
            self, registry, claude_dir):
        """The report has to converge too, not just the state."""
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a", model="<synthetic>", inp=0, out=0,
                       cache_read=0, cache_creation=0),
        ])

        first = backfill_usage(claude_config_dir=claude_dir, write=True)
        second = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert first.unparseable == 1 and first.repaired == 0
        assert second.unparseable == 1 and second.repaired == 0
        assert _state(registry, "s")["model_usage"] == {}

    def test_backfill_does_not_bump_last_activity(self, registry, claude_dir):
        """last_activity drives ordering and stall detection; a repair is not
        session activity."""
        _register(registry, "s", "sess-1")
        before = _state(registry, "s")["last_activity"]
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        assert _state(registry, "s")["last_activity"] == before

    def test_missing_transcript_is_reported_not_invented(self, registry,
                                                         claude_dir):
        _register(registry, "s", "sess-gone")

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.transcript_missing == 1
        assert report.repaired == 0
        assert _state(registry, "s")["model_usage"] == {}

    def test_session_without_session_id_is_skipped(self, registry, claude_dir):
        _register(registry, "s", "")
        report = backfill_usage(claude_config_dir=claude_dir, write=True)
        assert report.transcript_missing == 1

    def test_transcript_with_no_usage_is_unparseable(self, registry,
                                                     claude_dir):
        _write_transcript(claude_dir, "sess-1", ["{bad\n", "also bad\n"])
        _register(registry, "s", "sess-1")

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.unparseable == 1
        assert report.repaired == 0

    def test_mixed_model_transcript_records_each_model(self, registry,
                                                       claude_dir):
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a"),
            _assistant("msg-b", model="claude-haiku-4-5", inp=1, out=2,
                       cache_read=3, cache_creation=4),
        ])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        usage = _state(registry, "s")["model_usage"]
        assert usage[f"anthropic:{ASSISTANT_MODEL}"]["output_tokens"] == 10
        assert usage["anthropic:claude-haiku-4-5"]["output_tokens"] == 2
        # An ambiguous multi-model session gets no single `model` label.
        assert _state(registry, "s")["model"] == ""

    def test_non_anthropic_session_is_left_alone(self, registry, claude_dir):
        """A codex session's tokens never come from a Claude transcript."""
        _register(registry, "s", "sess-1", provider="openai")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.skipped == 1
        assert _state(registry, "s")["model_usage"] == {}

    def test_transcript_claimed_by_one_session_only(self, registry,
                                                    claude_dir):
        """Two registry rows pointing at one transcript must not each book its
        tokens - that would double-count the same spend."""
        _register(registry, "a", "sess-1")
        _register(registry, "b", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.repaired == 1
        assert report.skipped == 1
        booked = [n for n in ("a", "b") if _state(registry, n)["model_usage"]]
        assert len(booked) == 1

    def test_transcript_found_by_scan_when_cwd_slug_differs(self, registry,
                                                            claude_dir):
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")],
                          project="-some-other-slug")

        assert backfill_usage(claude_config_dir=claude_dir, write=True
                              ).repaired == 1


# --- the repaired state feeds the existing spend fold ----------------------


class TestRepairedUsageFoldsOnce:
    def test_repaired_tokens_reach_the_rollup_exactly_once(self, registry,
                                                           claude_dir,
                                                           tmp_path):
        from bobi import paths
        from bobi.costs import rollup_costs

        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a"), _assistant("msg-a"),
        ])
        backfill_usage(claude_config_dir=claude_dir, write=True)
        backfill_usage(claude_config_dir=claude_dir, write=True)

        summary = rollup_costs(paths.sessions_dir())
        tokens = summary.tokens_by_model[f"anthropic:{ASSISTANT_MODEL}"]
        assert tokens == {"input_tokens": 39_860,
                          "cached_input_tokens": 15_282,
                          "output_tokens": 10}

    def test_repaired_session_without_provider_cost_gets_only_an_estimate(
            self, registry, claude_dir):
        from bobi import paths
        from bobi.costs import PRICE_TABLE, estimate_cost, rollup_costs

        model = next(m.split(":", 1)[1] for m in PRICE_TABLE
                     if m.startswith("anthropic:"))
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a", model=model),
        ])
        backfill_usage(claude_config_dir=claude_dir, write=True)

        summary = rollup_costs(paths.sessions_dir())
        assert summary.total_cost_usd == 0.0
        assert summary.estimated_cost_usd == pytest.approx(estimate_cost(
            "anthropic", model, input_tokens=39_860, output_tokens=10,
            cached_input_tokens=15_282))

    def test_repaired_session_with_provider_cost_gets_no_estimate(
            self, registry, claude_dir):
        from bobi import paths
        from bobi.costs import rollup_costs

        _register(registry, "s", "sess-1", total_cost_usd=0.25,
                  model_usage={f"anthropic:{ASSISTANT_MODEL}": {
                      "cost_usd": 0.25, "input_tokens": 0,
                      "output_tokens": 0, "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])
        backfill_usage(claude_config_dir=claude_dir, write=True)

        summary = rollup_costs(paths.sessions_dir())
        assert summary.total_cost_usd == 0.25
        assert summary.estimated_cost_usd == 0.0
