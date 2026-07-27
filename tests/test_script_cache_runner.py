"""Runner / sandbox / lifecycle / breaker / notify tests for script_cache (#327).

These exercise the full check runner with the agent generation step injected
(no Claude CLI), the real sandbox, real file I/O, and the trusted-state sidecar.
"""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import bobi.monitors.script_cache_checks as sc
from bobi.monitors.script_cache_checks import (
    GenResult,
    run_sandboxed,
    script_cache,
)
from bobi.monitors.schema import Monitor


GOOD_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo '[{\"id\": \"item-1\", \"subject\": \"hi\"}]'\n"
)


PUBLISHED: list = []

# D004 moved generation onto a worker thread, and a tick now waits only
# GEN_HANDOFF_GRACE for it before detaching. Every test below that predates
# D004 uses a fake generator that returns in microseconds and asserts on the
# COMPLETED outcome, so racing a 2s wall-clock grace makes them fail purely
# under CPU load (observed: 12 failures while other builds shared the box).
# Give those tests a grace they cannot lose, so they stay assertions about
# script-cache behavior rather than about how busy the machine is.
# TestSchedulerThreadNotBlocked - the class that actually exercises the
# handoff - restores the real value.
REAL_GEN_HANDOFF_GRACE = sc.GEN_HANDOFF_GRACE
_SETTLED_GRACE = 60.0


@pytest.fixture()
def scripts_dir(tmp_path):
    """Redirect the script cache + trusted state into a temp dir, and silence
    the real publish/slack wire so notifications don't hit the network.

    Captured events are exposed as ``scripts_dir.events`` via the helper below."""
    d = tmp_path / "scripts"
    d.mkdir()
    PUBLISHED.clear()

    def fake_publish(event, data):
        PUBLISHED.append((event, data))
        return True

    with patch("bobi.monitors.script_cache_checks._scripts_dir", return_value=d), \
         patch("bobi.monitors.script_cache_checks.publish", side_effect=fake_publish), \
         patch("bobi.monitors.script_cache_checks._install_policy", return_value={}), \
         patch.object(sc, "GEN_HANDOFF_GRACE", _SETTLED_GRACE):
        yield d
    # Generation is dispatched to a worker thread and its result is held until
    # a later tick collects it - an uncollected one must not leak into the next
    # test (same monitor names are reused).
    sc._generations.clear()


def _events():
    return [e for e, _ in PUBLISHED]


def _monitor(name="email-watch", **extra):
    base = {"prompt": "Check my email for unread messages", "id_field": "id"}
    base.update(extra)
    return Monitor(name=name, check="script_cache",
                   event="monitor/email.received", extra=base)


class TestPinIsAtomicUnderConcurrency:
    """`_pin` must not keep its own tmp+rename beside the shared helper.

    `_save_trusted_state` in this same module adopted `fsutil` precisely
    because "its temp name carries pid + nanoseconds, so two writers racing on
    one sidecar cannot consume each other's temp" - and `_pin`, 230 lines
    below, kept writing a FIXED `.tmp-<name>.sh`. D004 is what makes that
    reachable: generation moved onto its own worker thread, so two generations
    for one monitor can now overlap where the scheduler used to serialize them.
    The loser's write lands under the winner's rename, so the active script no
    longer hashes to the `sha256` the winner stamped into trusted state and
    `_verify_integrity` refuses to run the monitor at all.
    """

    def test_racing_pins_leave_a_script_matching_its_recorded_hash(
            self, scripts_dir):
        import threading

        monitor = _monitor()
        env = sc.CapabilityEnvelope()
        start = threading.Barrier(8)
        errors: list = []
        states: list = []

        def pin(i: int):
            # Over `TextIOWrapper`'s 8192-byte buffer AND of differing length
            # per writer. At 1.3 KB every body fitted one `write()` syscall, so
            # a racing pair could only ever swap whole contents - the torn
            # write this asserts against was unreachable and the hash check was
            # decorative. Multi-syscall, ragged-length payloads can actually
            # interleave.
            body = (f"#!/bin/sh\necho '{{\"id\": \"{i}\"}}'\n"
                    + f"# pad {i}\n" * (2000 + i * 250))
            state: dict = {}
            try:
                start.wait(timeout=10)
                sc._pin(monitor.name, body, monitor, f"fp{i}", env, state)
            except Exception as e:              # noqa: BLE001 - reported below
                errors.append(e)
            else:
                states.append(state)

        threads = [threading.Thread(target=pin, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"a racing _pin raised: {errors}"
        active = sc._active_path(monitor.name)
        assert active.is_file(), "the active script went missing in the race"
        on_disk = sc._sha256(active.read_text())
        assert on_disk in {s["sha256"] for s in states}, (
            "the pinned script matches no writer's recorded hash - a torn "
            "write, so _verify_integrity would refuse to run this monitor"
        )

    def test_pin_leaves_no_temp_behind(self, scripts_dir):
        """Any leftover, not just one shaped like `fsutil`'s temp.

        Filtering on a `.tmp` SUFFIX only ever matched `fsutil`'s own
        `.<name>.<pid>.<ns>.tmp`, which `atomic_write_text` always renames or
        unlinks - so the check could fail only if `fsutil` were broken, never
        if `_pin` regressed to its old `.tmp-<name>.sh`. Assert the directory
        holds exactly the active script and nothing else.
        """
        monitor = _monitor()
        sc._pin(monitor.name, "#!/bin/sh\necho '{\"id\": \"1\"}'\n",
                monitor, "fp", sc.CapabilityEnvelope(), {})
        present = sorted(p.name for p in scripts_dir.iterdir())
        assert present == [sc._active_path(monitor.name).name], present


def _gen(items, script=GOOD_SCRIPT, success=True, cost=0.02):
    return lambda monitor, cwd, policy: GenResult(
        success=success, items=items, script=script, cost_usd=cost)


def _recording_gen(calls: list):
    """A generation stub for paths that must never reach the agent.

    It records instead of calling ``pytest.fail``: generation runs on a worker
    thread, where a failure raised inside the stub would never reach the test.
    Callers assert on ``calls`` from the main thread.
    """
    def _fn(monitor, cwd, policy):
        calls.append(monitor.name)
        return GenResult(success=False, error="agent must not have been called")
    return _fn


def _state(scripts_dir, name="email-watch"):
    p = scripts_dir / f"{name}.state.json"
    return json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class TestSandbox:
    def _content(self, body):
        return "#!/usr/bin/env bash\nset -euo pipefail\n" + body

    def test_home_and_tmpdir_redirected_to_scratch(self, scripts_dir):
        cp = run_sandboxed(self._content("echo \"$HOME\"\n"), dict(os.environ), 10)
        assert cp is not None and cp.returncode == 0
        home = cp.stdout.strip()
        assert "msc-" in home  # HOME points into the disposable scratch

    def test_scratch_cleaned_up(self, scripts_dir):
        cp = run_sandboxed(self._content("pwd\n"), dict(os.environ), 10)
        scratch = cp.stdout.strip()
        assert not Path(scratch).exists()  # rmtree'd in finally

    def test_relative_write_lands_in_scratch_not_cwd(self, scripts_dir):
        # writing a relative file works (scratch is writable) but cannot escape;
        # the sandbox runs even an unvalidated script (validator is the gate) —
        # the point is it does not touch the manager's CWD
        before = set(os.listdir("."))
        run_sandboxed(self._content("echo hi > out.txt && cat out.txt\n"),
                      dict(os.environ), 10)
        assert set(os.listdir(".")) == before

    @pytest.mark.skipif(os.name != "posix", reason="RLIMIT is POSIX-only")
    def test_fsize_limit_blocks_large_write(self, scripts_dir):
        # a 50MB write exceeds the 10MB RLIMIT_FSIZE → script killed/fails
        cp = run_sandboxed(
            self._content("head -c 50000000 /dev/zero | tr '\\0' 'a' > big.txt\n"),
            dict(os.environ), 20)
        assert cp is None or cp.returncode != 0

    def test_timeout_kills_sleeper(self, scripts_dir):
        cp = run_sandboxed(self._content("sleep 10\n"), dict(os.environ), 1)
        assert cp is None  # TimeoutExpired → None


# ---------------------------------------------------------------------------
# First-gen + cached fast path
# ---------------------------------------------------------------------------

class TestFirstGenAndCache:
    def test_first_run_generates_pins_and_notifies(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "item-1"}])):
            result = script_cache(m, [Path("/repo")])
        assert result is not None and len(result) == 1
        assert result[0].key == "item-1"
        # pinned
        assert (scripts_dir / "email-watch.sc.sh").exists()
        st = _state(scripts_dir)
        assert st["last_mode"] == "first_gen"
        assert st["sha256"] and st["fingerprint"]
        # REAL post-hoc notification fired (PROCEED-BUT-NOTIFY)
        assert "monitor/script.first_run" in _events()

    def test_second_run_uses_cache_at_zero_cost(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "item-1"}])):
            script_cache(m, [Path("/repo")])
        # second run must NOT call the agent
        calls: list = []
        with patch.object(sc, "generate_candidate", _recording_gen(calls)):
            result = script_cache(m, [Path("/repo")])
        assert not calls, "agent called on the cached path"
        assert result is not None and result[0].key == "item-1"
        st = _state(scripts_dir)
        assert st["last_mode"] == "cached"
        assert st["last_tick"]["cost_usd"] == 0.0
        assert st["cached_runs"] == 1

    def test_missing_prompt_is_indeterminate(self, scripts_dir):
        m = Monitor(name="bad", check="script_cache", extra={})
        assert script_cache(m, [Path("/repo")]) is None


# ---------------------------------------------------------------------------
# Self-heal
# ---------------------------------------------------------------------------

class TestSelfHeal:
    def test_broken_cache_falls_back_and_repins(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "item-1"}])):
            script_cache(m, [Path("/repo")])
        # corrupt the active script on disk
        active = scripts_dir / "email-watch.sc.sh"
        active.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 1\n")
        # TOCTOU/integrity check will refuse it (hash mismatch) → self-heal
        healed = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "echo '[{\"id\": \"item-2\"}]'\n"
        )
        with patch.object(sc, "generate_candidate", _gen([{"id": "item-2"}], script=healed)):
            result = script_cache(m, [Path("/repo")])
        assert result is not None and result[0].key == "item-2"
        st = _state(scripts_dir)
        assert st["last_mode"] == "fallback_regen"
        # re-pinned with the new content's hash
        assert sc._sha256((scripts_dir / "email-watch.sc.sh").read_text()) == st["sha256"]

    def test_self_heal_returns_items_even_when_candidate_unpinnable(self, scripts_dir):
        # candidate is invalid (off-allowlist binary) → not pinned, but the
        # agent run still produces this tick's items (never a wasted tick)
        m = _monitor()
        bad = "#!/usr/bin/env bash\nset -euo pipefail\npsql -c 'select 1'\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=bad)):
            result = script_cache(m, [Path("/repo")])
        assert result is not None and result[0].key == "x"
        assert not (scripts_dir / "email-watch.sc.sh").exists()
        assert _state(scripts_dir)["script_regen_fails"] == 1

    def test_garbage_smoke_output_not_pinned(self, scripts_dir):
        m = _monitor()
        garbage = "#!/usr/bin/env bash\nset -euo pipefail\necho 'not json at all'\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=garbage)):
            script_cache(m, [Path("/repo")])
        assert not (scripts_dir / "email-watch.sc.sh").exists()


# ---------------------------------------------------------------------------
# TOCTOU
# ---------------------------------------------------------------------------

class TestTOCTOU:
    def test_tampered_script_refused(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "item-1"}])):
            script_cache(m, [Path("/repo")])
        # tamper: same exit-0 but mutate content so sha differs from trusted state
        active = scripts_dir / "email-watch.sc.sh"
        active.write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                          "echo '[{\"id\": \"tampered\"}]'\n")
        # generation is stubbed to a DIFFERENT known-good script; if the tampered
        # script ran, we'd see 'tampered'. We must instead self-heal.
        heal = "#!/usr/bin/env bash\nset -euo pipefail\necho '[{\"id\": \"healed\"}]'\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "healed"}], script=heal)):
            result = script_cache(m, [Path("/repo")])
        assert result[0].key == "healed"  # tampered content never executed

    def test_revalidation_refuses_now_invalid_script(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "ok"}])):
            script_cache(m, [Path("/repo")])
        st = _state(scripts_dir)
        # craft a malicious script and update trusted sha to match (hash passes)
        evil = "#!/usr/bin/env bash\nset -euo pipefail\nrm -rf /tmp/x\n"
        active = scripts_dir / "email-watch.sc.sh"
        active.write_text(evil)
        st["sha256"] = sc._sha256(evil)
        (scripts_dir / "email-watch.state.json").write_text(json.dumps(st))
        # re-validation (re-run each tick) must reject the rm → self-heal
        heal = "#!/usr/bin/env bash\nset -euo pipefail\necho '[{\"id\": \"safe\"}]'\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "safe"}], script=heal)):
            result = script_cache(m, [Path("/repo")])
        assert result[0].key == "safe"


# ---------------------------------------------------------------------------
# Fingerprint invalidation + recache
# ---------------------------------------------------------------------------

class TestInvalidation:
    def test_fingerprint_change_regenerates(self, scripts_dir):
        with patch.object(sc, "generate_candidate", _gen([{"id": "a"}])):
            script_cache(_monitor(), [Path("/repo")])
        # change the prompt → fingerprint mismatch → regenerate
        m2 = _monitor(prompt="Check my email for STARRED messages")
        heal = "#!/usr/bin/env bash\nset -euo pipefail\necho '[{\"id\": \"b\"}]'\n"
        calls = []
        def gen(monitor, cwd, policy):
            calls.append(1)
            return GenResult(True, items=[{"id": "b"}], script=heal)
        with patch.object(sc, "generate_candidate", gen):
            result = script_cache(m2, [Path("/repo")])
        assert calls and result[0].key == "b"

    def test_max_age_refreshes(self, scripts_dir):
        m = _monitor(max_age="1s")
        with patch.object(sc, "generate_candidate", _gen([{"id": "a"}])):
            script_cache(m, [Path("/repo")])
        time.sleep(1.1)
        calls = []
        heal = "#!/usr/bin/env bash\nset -euo pipefail\necho '[{\"id\": \"a\"}]'\n"
        def gen(monitor, cwd, policy):
            calls.append(1)
            return GenResult(True, items=[{"id": "a"}], script=heal)
        with patch.object(sc, "generate_candidate", gen):
            script_cache(m, [Path("/repo")])
        assert calls  # regenerated because the pinned script aged out


# ---------------------------------------------------------------------------
# Approval modes
# ---------------------------------------------------------------------------

class TestApprovalModes:
    def test_auto_pins_directly(self, scripts_dir):
        m = _monitor(approval="auto")
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}])):
            script_cache(m, [Path("/repo")])
        assert (scripts_dir / "email-watch.sc.sh").exists()

    def test_review_queues_and_keeps_agent_runtime(self, scripts_dir):
        m = _monitor(approval="review")
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}])):
            result = script_cache(m, [Path("/repo")])
        # not pinned; queued to pending/ + review event fired; agent items returned
        assert not (scripts_dir / "email-watch.sc.sh").exists()
        assert (scripts_dir / "pending" / "email-watch.sh").exists()
        assert "monitor/script.review_requested" in _events()
        assert result[0].key == "x"

    def test_off_never_persists(self, scripts_dir):
        m = _monitor(approval="off")
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}])):
            script_cache(m, [Path("/repo")])
            # run again — still no script, agent every time
            script_cache(m, [Path("/repo")])
        assert not (scripts_dir / "email-watch.sc.sh").exists()
        assert not (scripts_dir / "pending" / "email-watch.sh").exists()

    def test_review_self_heal_in_envelope_auto_promotes(self, scripts_dir):
        # Under review mode an approved script's self-heal that stays inside the
        # recorded capability envelope auto-promotes (a mechanical repair). The
        # smoke run hits the network for gh, so we stub it — smoke is covered by
        # its own test; here we exercise the envelope/approval routing.
        from bobi.monitors.script_cache_checks import approve_pending
        m = _monitor(approval="review")
        s1 = "#!/usr/bin/env bash\nset -euo pipefail\ngh pr list --json number\n"
        with patch.object(sc, "_smoke_ok", return_value=True), \
             patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=s1)):
            script_cache(m, [Path("/repo")])  # review → queued to pending/
            assert (scripts_dir / "pending" / "email-watch.sh").exists()
            approve_pending(m, scripts_dir)   # human promotes it
            assert (scripts_dir / "email-watch.sc.sh").exists()
            assert _state(scripts_dir).get("envelope")
            # force a self-heal (integrity fail) with an in-envelope repair
            (scripts_dir / "email-watch.sc.sh").write_text("broken")
            s2 = "#!/usr/bin/env bash\nset -euo pipefail\ngh pr list --json number,title\n"
            with patch.object(sc, "generate_candidate", _gen([{"id": "y"}], script=s2)):
                script_cache(m, [Path("/repo")])
        # auto re-pinned (same binary set → inside the envelope)
        assert sc._sha256((scripts_dir / "email-watch.sc.sh").read_text()) == \
            _state(scripts_dir)["sha256"]

    def test_review_self_heal_new_capability_reenters_review(self, scripts_dir):
        from bobi.monitors.script_cache_checks import approve_pending
        m = _monitor(approval="review", allow_http=True, http_hosts=["api.example.com"])
        s1 = "#!/usr/bin/env bash\nset -euo pipefail\ngh pr list --json number\n"
        with patch.object(sc, "_smoke_ok", return_value=True), \
             patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=s1)):
            script_cache(m, [Path("/repo")])
            approve_pending(m, scripts_dir)  # consumes the pending file
            assert not (scripts_dir / "pending" / "email-watch.sh").exists()
            (scripts_dir / "email-watch.sc.sh").write_text("broken")
            # self-heal introduces curl (a NEW binary/host) → must re-enter review
            s2 = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                  "curl https://api.example.com/x\n")
            with patch.object(sc, "generate_candidate", _gen([{"id": "y"}], script=s2)):
                script_cache(m, [Path("/repo")])
        # not auto-promoted: queued to pending again + review event fired
        assert (scripts_dir / "pending" / "email-watch.sh").exists()
        assert "monitor/script.review_requested" in _events()


# ---------------------------------------------------------------------------
# Scheduler-thread budget
# ---------------------------------------------------------------------------

class TestSchedulerThreadNotBlocked:
    """Generation never runs on the caller's thread for long (D004).

    The scheduler evaluates every monitor from ONE thread, so a self-heal that
    blocks on the agent runtime (``run_check_blocking``: 2 attempts x 600s)
    stalls every other monitor for up to ~20 minutes - interval monitors drift
    and a weekday-gated ``at:`` slot that passes during the block is skipped as
    a missed-while-down catch-up. Detection goes out-of-band instead, exactly
    like the description-only check flavor.
    """

    @pytest.fixture(autouse=True)
    def real_grace(self, scripts_dir):
        """This class is about the handoff itself, so it needs the production
        grace - not the generous one the other tests run under."""
        with patch.object(sc, "GEN_HANDOFF_GRACE", REAL_GEN_HANDOFF_GRACE):
            yield

    def test_slow_generation_does_not_hold_the_tick(self, scripts_dir):
        m = _monitor()
        started = threading.Event()
        release = threading.Event()

        def slow_gen(monitor, cwd, policy):
            # A real generation runs for minutes; this one runs until the test
            # says stop, so an inline call cannot sneak under the assertion.
            started.set()
            release.wait(30)
            return GenResult(success=True, items=[{"id": "item-1"}],
                             script=GOOD_SCRIPT, cost_usd=0.02)

        with patch.object(sc, "generate_candidate", slow_gen):
            t0 = time.monotonic()
            first = script_cache(m, [Path("/repo")])
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0, f"tick held the caller for {elapsed:.1f}s"
            assert started.is_set()
            assert first is None  # indeterminate: detection is in flight

            # It finishes out-of-band; the next tick collects its items.
            release.set()
            gen = sc._generations[m.name]
            assert gen.done.wait(20)
            second = script_cache(m, [Path("/repo")])

        assert [c.key for c in second] == ["item-1"]
        assert (scripts_dir / "email-watch.sc.sh").exists()  # pinned by the worker
        st = _state(scripts_dir)
        assert st["last_mode"] == "first_gen"
        assert st["sha256"] and st["fingerprint"]

    def test_in_flight_generation_is_not_restarted_by_later_ticks(self, scripts_dir):
        m = _monitor()
        release = threading.Event()
        calls = []

        def blocking_gen(monitor, cwd, policy):
            calls.append(1)
            release.wait(20)
            return GenResult(success=True, items=[{"id": "x"}],
                             script=GOOD_SCRIPT, cost_usd=0.02)

        with patch.object(sc, "generate_candidate", blocking_gen):
            assert script_cache(m, [Path("/repo")]) is None
            assert script_cache(m, [Path("/repo")]) is None
            assert len(calls) == 1, "a second agent run was started"
            release.set()
            assert sc._generations[m.name].done.wait(20)
            collected = script_cache(m, [Path("/repo")])

        assert [c.key for c in collected] == ["x"]
        assert m.name not in sc._generations  # collected once, then dropped

    def test_failed_generation_is_indeterminate_and_retries_next_tick(self,
                                                                      scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate",
                          _gen(None, success=False)):
            assert script_cache(m, [Path("/repo")]) is None
        assert _state(scripts_dir)["script_regen_fails"] == 1
        # the failure is not sticky: the next tick generates again
        with patch.object(sc, "generate_candidate", _gen([{"id": "later"}])):
            result = script_cache(m, [Path("/repo")])
        assert [c.key for c in result] == ["later"]


# ---------------------------------------------------------------------------
# Circuit breaker + backoff
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_persistent_failure_fires_alert_and_backs_off(self, scripts_dir):
        m = _monitor()
        # every generation fails to produce a valid script
        bad = "#!/usr/bin/env bash\nset -euo pipefail\npsql -c x\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=bad)):
            for _ in range(sc.SCRIPT_REGEN_MAX):
                script_cache(m, [Path("/repo")])
        st = _state(scripts_dir)
        assert st["script_regen_fails"] >= sc.SCRIPT_REGEN_MAX
        assert st.get("backoff_until")
        assert "monitor/script.failing" in _events()

    def test_backoff_skips_agent_call(self, scripts_dir):
        m = _monitor()
        st0 = {"backoff_until": (sc._now().replace(year=sc._now().year + 1)).isoformat()}
        (scripts_dir / "email-watch.state.json").write_text(json.dumps(st0))
        calls: list = []
        with patch.object(sc, "generate_candidate", _recording_gen(calls)):
            assert script_cache(m, [Path("/repo")]) is None
        assert not calls, "agent called during backoff"

    def test_counter_resets_on_success(self, scripts_dir):
        m = _monitor()
        (scripts_dir / "email-watch.state.json").write_text(
            json.dumps({"script_regen_fails": 2}))
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}])):
            script_cache(m, [Path("/repo")])
        assert _state(scripts_dir)["script_regen_fails"] == 0

    def test_pause_policy_pauses_instead_of_backoff(self, scripts_dir):
        m = _monitor(on_persistent_failure="pause")
        bad = "#!/usr/bin/env bash\nset -euo pipefail\npsql -c x\n"
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}], script=bad)):
            for _ in range(sc.SCRIPT_REGEN_MAX):
                script_cache(m, [Path("/repo")])
        assert _state(scripts_dir).get("paused") is True
        # paused → subsequent ticks are indeterminate and skip the agent
        calls: list = []
        with patch.object(sc, "generate_candidate", _recording_gen(calls)):
            assert script_cache(m, [Path("/repo")]) is None
        assert not calls, "agent called while paused"


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

class TestObservability:
    def test_counters_accumulate(self, scripts_dir):
        m = _monitor()
        with patch.object(sc, "generate_candidate", _gen([{"id": "x"}], cost=0.05)):
            script_cache(m, [Path("/repo")])  # first_gen
        for _ in range(3):
            script_cache(m, [Path("/repo")])  # cached x3
        st = _state(scripts_dir)
        assert st["cached_runs"] == 3
        assert st["fallback_runs"] == 1
        assert st["total_agent_cost_usd"] == pytest.approx(0.05)
        assert st["last_mode"] == "cached"


# ---------------------------------------------------------------------------
# Trusted-state durability
# ---------------------------------------------------------------------------

class TestTrustedStateDurability:
    """The sidecar holds the security baseline (pinned sha256 + envelope), so
    it goes through the one durable-write path rather than a second, weaker
    tmp+rename of its own."""

    def test_kill_mid_write_keeps_the_state_and_orphans_no_temp(self, scripts_dir):
        """A partial `.tmp` left behind is the fixed-name hazard made real.

        Only OSError was caught, so a KeyboardInterrupt (Ctrl-C, supervisor
        restart) escaped with the half-written temp still on disk, where the
        next writer's fixed name collides with it.
        """
        sc._save_trusted_state("email-watch", {"sha256": "baseline", "gen": 1})
        before = (scripts_dir / "email-watch.state.json").read_bytes()

        real_write_text = Path.write_text

        def killed(self, data, *a, **kw):
            real_write_text(self, data[:12], *a, **kw)
            raise KeyboardInterrupt("simulated kill mid-write")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "write_text", killed)
            with pytest.raises(KeyboardInterrupt):
                sc._save_trusted_state("email-watch", {"sha256": "new", "gen": 2})

        assert (scripts_dir / "email-watch.state.json").read_bytes() == before
        assert [p.name for p in scripts_dir.iterdir()] == ["email-watch.state.json"]

    def test_concurrent_writers_do_not_share_a_temp(self, scripts_dir):
        """A fixed `.json.tmp` lets one writer's rename consume the other's.

        The loser's `os.replace` then fails on a file it really did write, and
        the OSError handler downgrades that lost write to a log line.
        """
        seen = {"nested": False}
        real_replace = os.replace

        def hooked(src, dst):
            if not seen["nested"]:
                seen["nested"] = True
                sc._save_trusted_state("email-watch", {"sha256": "B"})
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", hooked)
            sc._save_trusted_state("email-watch", {"sha256": "A"})

        assert seen["nested"] is True
        assert _state(scripts_dir) == {"sha256": "A"}   # A's rename landed intact
        assert [p.name for p in scripts_dir.iterdir()] == ["email-watch.state.json"]
