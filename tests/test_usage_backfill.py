"""Tests for the historical session-usage backfill (#935).

Sessions recorded before the camel-case normalization fix carry provider
dollars with every token counter at zero, or nothing at all. The backfill
recovers token telemetry from Claude's retained JSONL transcripts and fills
ONLY what is missing, never overwriting a recorded fact.
"""

import json
import os

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
    kw.setdefault("status", "completed")
    registry.register(SessionEntry(
        name=name, session_id=session_id, cwd="/work", **kw,
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

    def test_transcript_truncated_mid_character_is_recoverable(self,
                                                               claude_dir):
        """A session killed by a crash, OOM, or full disk leaves its transcript
        cut mid-multibyte. The usable prefix must still be recovered."""
        d = claude_dir / "projects" / "-work"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "s1.jsonl"
        good = (json.dumps(_assistant("msg-a")) + "\n").encode()
        path.write_bytes(good + '{"type":"assistant","x":"'.encode() + b"\xf0\x9f")

        assert transcript_usage(path)[ASSISTANT_MODEL]["output_tokens"] == 10

    def test_non_regular_file_is_not_read(self, claude_dir, tmp_path):
        """A <uuid>.jsonl that is a FIFO would block the backfill forever."""
        d = claude_dir / "projects" / "-work"
        d.mkdir(parents=True, exist_ok=True)
        fifo = d / "s1.jsonl"
        os.mkfifo(fifo)

        with pytest.raises(OSError):
            transcript_usage(fifo)


# --- backfill --------------------------------------------------------------


class TestBackfill:
    def test_dry_run_makes_no_filesystem_changes(self, registry, claude_dir):
        """Not just 'state.json is unchanged' - no file appears either. Taking
        the state lock would create a .lock file, which is a filesystem change
        the dry run promises not to make."""
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])
        d = registry.session_dir("s")
        before = {p.name: p.read_bytes() for p in d.iterdir()}

        report = backfill_usage(claude_config_dir=claude_dir, write=False)

        assert report.repaired == 1
        assert {p.name: p.read_bytes() for p in d.iterdir()} == before

    def test_one_unusable_transcript_does_not_abort_the_run(self, registry,
                                                            claude_dir):
        """Sessions are walked oldest-first, so an early bad transcript must
        not strand every session behind it."""
        _register(registry, "old", "sess-bad", started_at=1.0)
        _register(registry, "new", "sess-good", started_at=2.0)
        bad = claude_dir / "projects" / "-work"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "sess-bad.jsonl").write_bytes(b'\xff\xfe not utf-8 at all')
        _write_transcript(claude_dir, "sess-good", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.repaired == 1
        assert _state(registry, "new")["model_usage"] != {}

    def test_cwd_wins_over_a_shadowing_transcript(self, registry, claude_dir):
        """Transcripts are matched by filename, so a same-named file in an
        alphabetically-earlier project dir would otherwise shadow the real
        one. The session's own cwd is what disambiguates."""
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [
            _assistant("shadow", inp=999, out=999, cache_read=0,
                       cache_creation=0),
        ], project="-aaa-earlier")
        _write_transcript(claude_dir, "sess-1", [_assistant("real")],
                          project="-work")

        backfill_usage(claude_config_dir=claude_dir, write=True)

        entry = _state(registry, "s")["model_usage"][
            f"anthropic:{ASSISTANT_MODEL}"]
        assert entry["output_tokens"] == 10, "shadow transcript won"

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

    def test_fills_the_existing_key_for_a_model_variant(self, registry,
                                                        claude_dir):
        """The live writer keys by the SDK's model_usage dict key, which carries
        variant markers the transcript's `message.model` does not - the real
        registry holds `anthropic:claude-opus-4-8[1m]` while its transcript says
        `claude-opus-4-8`. Adding a second key would split one model across two
        rows in `costs --by model`, and _has_tokens makes that a one-way door.
        """
        variant = f"anthropic:{ASSISTANT_MODEL}[1m]"
        _register(registry, "s", "sess-1", total_cost_usd=0.25,
                  model_usage={variant: {"cost_usd": 0.25, "input_tokens": 0,
                                         "output_tokens": 0,
                                         "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        usage = _state(registry, "s")["model_usage"]
        assert list(usage) == [variant], "recovered usage split onto a new key"
        assert usage[variant]["output_tokens"] == 10
        assert usage[variant]["cost_usd"] == 0.25

    def test_fills_the_existing_key_for_a_dated_snapshot(self, registry,
                                                         claude_dir):
        """The mirror case: registry carries the dated snapshot, transcript the
        bare alias."""
        dated = "anthropic:claude-haiku-4-5-20251001"
        _register(registry, "s", "sess-1",
                  model_usage={dated: {"cost_usd": 0.0, "input_tokens": 0,
                                       "output_tokens": 0,
                                       "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a", model="claude-haiku-4-5"),
        ])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        assert list(_state(registry, "s")["model_usage"]) == [dated]

    def test_a_genuinely_different_model_gets_its_own_key(self, registry,
                                                          claude_dir):
        """Variant-matching must not collapse distinct models."""
        other = f"anthropic:{ASSISTANT_MODEL}"
        _register(registry, "s", "sess-1",
                  model_usage={other: {"cost_usd": 0.0, "input_tokens": 0,
                                       "output_tokens": 0,
                                       "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [
            _assistant("msg-a", model="claude-haiku-4-5"),
        ])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        assert set(_state(registry, "s")["model_usage"]) == {
            other, "anthropic:claude-haiku-4-5"}

    def test_existing_usage_is_never_overwritten(self, registry, claude_dir):
        _register(registry, "s", "sess-1",
                  model_usage={f"anthropic:{ASSISTANT_MODEL}": {
                      "cost_usd": 1.0, "input_tokens": 7,
                      "output_tokens": 3, "cached_input_tokens": 1}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

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

        assert first.unusable == 1 and first.repaired == 0
        assert second.unusable == 1 and second.repaired == 0
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

    def test_transcript_with_no_usage_is_unusable(self, registry,
                                                     claude_dir):
        _write_transcript(claude_dir, "sess-1", ["{bad\n", "also bad\n"])
        _register(registry, "s", "sess-1")

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.unusable == 1
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


# --- CLI wiring ------------------------------------------------------------


class TestCostsCli:
    """`costs` became a click group to host `backfill`. The bare form is
    documented in skills/bobi.md, docs/QUICKSTART.md and docs/MONITORS.md, so a
    regression there is user-visible and silent."""

    def _run(self, bobi_install, *args):
        from click.testing import CliRunner
        from bobi.cli import main
        from tests.conftest import TEST_AGENT_NAME
        return CliRunner().invoke(
            main, ["agent", TEST_AGENT_NAME, "costs", *args])

    def test_bare_costs_still_runs(self, bobi_install):
        result = self._run(bobi_install)
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize("dimension",
                             ["provider", "model", "session", "role"])
    def test_by_dimension_still_runs(self, bobi_install, dimension):
        result = self._run(bobi_install, "--by", dimension)
        assert result.exit_code == 0, result.output

    def test_backfill_is_registered(self, bobi_install):
        result = self._run(bobi_install, "--help")
        assert result.exit_code == 0, result.output
        assert "backfill" in result.output

    def test_backfill_defaults_to_a_dry_run(self, bobi_install, claude_dir):
        registry = SessionRegistry()
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        result = self._run(bobi_install, "backfill",
                           "--claude-config-dir", str(claude_dir))

        assert result.exit_code == 0, result.output
        assert "Would repair" in result.output
        assert _state(registry, "s")["model_usage"] == {}

    def test_backfill_write_applies(self, bobi_install, claude_dir):
        registry = SessionRegistry()
        _register(registry, "s", "sess-1")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        result = self._run(bobi_install, "backfill", "--write",
                           "--claude-config-dir", str(claude_dir))

        assert result.exit_code == 0, result.output
        assert _state(registry, "s")["model_usage"] != {}

    def test_write_and_dry_run_are_mutually_exclusive(self, bobi_install):
        result = self._run(bobi_install, "backfill", "--write", "--dry-run")
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestRepairDoesNotCorrupt:
    """Failures found by driving the real flow, each of which silently made
    the recorded numbers worse rather than better."""

    def test_distinct_models_are_not_merged(self):
        from bobi.sdk import _same_model
        assert _same_model("claude-opus-4-8[1m]", "claude-opus-4-8")
        assert _same_model("claude-haiku-4-5-20251001", "claude-haiku-4-5")
        # A version bump is a different model, not a snapshot of the same one.
        assert not _same_model("claude-sonnet-4", "claude-sonnet-4-5")
        assert not _same_model("claude-opus-4", "claude-opus-4-8")
        assert not _same_model("claude-opus-4-8", "claude-opus-5")

    def test_aggregate_only_dollars_survive_the_repair(self, registry,
                                                       claude_dir):
        """A session can record dollars with no per-model breakdown. Populating
        model_usage must not drop those dollars out of `costs --by model`."""
        from bobi import paths
        from bobi.costs import rollup_costs

        _register(registry, "s", "sess-1", total_cost_usd=2.5,
                  provider="anthropic", model=ASSISTANT_MODEL)
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])
        before = rollup_costs(paths.sessions_dir()).by_model

        backfill_usage(claude_config_dir=claude_dir, write=True)

        after = rollup_costs(paths.sessions_dir()).by_model
        assert before == {f"anthropic:{ASSISTANT_MODEL}": 2.5}
        assert after == before, "repair dropped per-model dollar attribution"

    def test_a_running_session_is_not_repaired(self, registry, claude_dir):
        """The transcript already holds an in-flight turn that record_cost has
        not booked yet. Setting counters now, then letting the turn add them,
        double-counts - and _has_tokens makes that unrepairable."""
        _register(registry, "live", "sess-1", status="running")
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.skipped == 1
        assert report.repaired == 0
        assert _state(registry, "live")["model_usage"] == {}

    def test_partial_post_fix_recording_is_reported_not_hidden(self, registry,
                                                               claude_dir):
        """The fix and the backfill ship together, so a long-lived agent can
        book one post-fix turn before the operator runs the repair. Its earlier
        history is then visible in the transcript but not recoverable without
        overwriting a recorded number, which this repair never does. That cost
        must be REPORTED, not disguised as a clean `already_populated`."""
        key = f"anthropic:{ASSISTANT_MODEL}"
        _register(registry, "s", "sess-1", total_cost_usd=905.0,
                  model_usage={key: {"cost_usd": 905.0, "input_tokens": 40,
                                     "output_tokens": 1,
                                     "cached_input_tokens": 30}})
        _write_transcript(claude_dir, "sess-1", [
            _assistant("old-1"), _assistant("old-2", inp=3),
        ])

        report = backfill_usage(claude_config_dir=claude_dir, write=True)

        assert report.partially_recorded == 1
        assert report.already_populated == 0
        assert "Partially recorded" in report.render(write=True)
        # The recorded numbers are still untouched.
        assert _state(registry, "s")["model_usage"][key]["output_tokens"] == 1

    def test_a_shorter_transcript_never_shrinks_recorded_usage(self, registry,
                                                              claude_dir):
        """A compaction fork leaves the retained transcript holding less than
        was recorded. Recovered numbers only ever complete, never reduce."""
        key = f"anthropic:{ASSISTANT_MODEL}"
        _register(registry, "s", "sess-1",
                  model_usage={key: {"cost_usd": 0.0, "input_tokens": 999_999,
                                     "output_tokens": 999,
                                     "cached_input_tokens": 0}})
        _write_transcript(claude_dir, "sess-1", [_assistant("msg-a")])

        backfill_usage(claude_config_dir=claude_dir, write=True)

        entry = _state(registry, "s")["model_usage"][key]
        assert entry["input_tokens"] == 999_999
        assert entry["output_tokens"] == 999
