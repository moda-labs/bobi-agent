"""Runner / sandbox / lifecycle / breaker / notify tests for script_cache (#327).

These exercise the full check runner with the agent generation step injected
(no Claude CLI), the real sandbox, real file I/O, and the trusted-state sidecar.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import modastack.monitors.script_cache_checks as sc
from modastack.monitors.script_cache_checks import (
    GenResult,
    run_sandboxed,
    script_cache,
)
from modastack.monitors.schema import Monitor


GOOD_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo '[{\"id\": \"item-1\", \"subject\": \"hi\"}]'\n"
)


PUBLISHED: list = []


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

    with patch("modastack.monitors.script_cache_checks._scripts_dir", return_value=d), \
         patch("modastack.monitors.script_cache_checks.publish", side_effect=fake_publish), \
         patch("modastack.monitors.script_cache_checks._install_policy", return_value={}):
        yield d


def _events():
    return [e for e, _ in PUBLISHED]


def _monitor(name="email-watch", **extra):
    base = {"prompt": "Check my email for unread messages", "id_field": "id"}
    base.update(extra)
    return Monitor(name=name, check="script_cache",
                   event="monitor/email.received", extra=base)


def _gen(items, script=GOOD_SCRIPT, success=True, cost=0.02):
    return lambda monitor, cwd, policy: GenResult(
        success=success, items=items, script=script, cost_usd=cost)


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
        boom = lambda *a, **k: pytest.fail("agent called on cached path")
        with patch.object(sc, "generate_candidate", boom):
            result = script_cache(m, [Path("/repo")])
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
        from modastack.monitors.script_cache_checks import approve_pending
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
        from modastack.monitors.script_cache_checks import approve_pending
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
        boom = lambda *a, **k: pytest.fail("agent called during backoff")
        with patch.object(sc, "generate_candidate", boom):
            assert script_cache(m, [Path("/repo")]) is None

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
        boom = lambda *a, **k: pytest.fail("agent called while paused")
        with patch.object(sc, "generate_candidate", boom):
            assert script_cache(m, [Path("/repo")]) is None


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
