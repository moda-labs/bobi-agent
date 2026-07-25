"""Integration tests for tool_poll script caching — real subprocess + real I/O.

Unlike the unit tests in test_tool_poll.py (which mock subprocess.run),
these tests exercise the full cache lifecycle with real shell commands,
real file I/O, and real scheduler reconciliation.  No Claude CLI needed.
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi.monitors.schema import Condition, Monitor
from bobi.monitors.tool_checks import (
    _run_command,
    _script_path,
    tool_poll,
)


@pytest.fixture()
def scripts_dir(tmp_path):
    """Redirect the script cache to a temp directory."""
    d = tmp_path / "scripts"
    d.mkdir()
    with patch("bobi.monitors.tool_checks._scripts_dir", return_value=d):
        yield d


def _write_cached_script(script: Path, cmd: list[str], body: str) -> None:
    """Write a cached script for `cmd` whose command line is `body`.

    The header stamp is what ties a cached script to the monitor's resolved
    command; an unstamped script is (correctly) retired as stale, which is a
    different code path than these tests exercise — see
    tests/test_tool_poll.py::TestCacheConfigFingerprint.
    """
    from bobi.monitors.tool_checks import (
        _FINGERPRINT_MARKER,
        _command_fingerprint,
    )

    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"{_FINGERPRINT_MARKER}{_command_fingerprint(cmd)}\n{body}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


class TestScriptCacheIntegration:
    """End-to-end script caching with real subprocess calls."""

    def test_first_run_caches_and_second_run_uses_cache(self, scripts_dir):
        """First run executes the command and caches it; second run uses the cached script."""
        cmd = ["echo", json.dumps([{"id": "item-1", "subject": "hello"}])]
        env = dict(os.environ)

        # First run — direct execution, should cache
        result1 = _run_command(cmd, env, 10, "cache-test", "id")
        assert result1 is not None
        assert len(result1) == 1
        assert result1[0].key == "item-1"

        # Verify the script was cached
        script = scripts_dir / "cache-test.sh"
        assert script.exists()
        assert script.stat().st_mode & stat.S_IEXEC
        content = script.read_text()
        assert "echo" in content
        assert "item-1" in content

        # Second run — should use cached script (same result)
        result2 = _run_command(cmd, env, 10, "cache-test", "id")
        assert result2 is not None
        assert len(result2) == 1
        assert result2[0].key == "item-1"

    def test_mutated_cache_returns_cached_data(self, scripts_dir):
        """If the cached script is mutated, the runner returns the mutated output."""
        cmd = ["echo", json.dumps([{"id": "original"}])]
        env = dict(os.environ)

        # First run — caches the script
        _run_command(cmd, env, 10, "mutate-test", "id")
        script = scripts_dir / "mutate-test.sh"
        assert script.exists()

        # Mutate the cached script to return different data
        _write_cached_script(script, cmd, 'echo \'[{"id": "mutated"}]\'')

        # Second run — uses mutated cached script
        result = _run_command(cmd, env, 10, "mutate-test", "id")
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "mutated"

    def test_broken_cache_falls_back_and_self_heals(self, scripts_dir):
        """A broken cached script triggers fallback to direct execution and re-caching."""
        cmd = ["echo", json.dumps([{"id": "healthy"}])]
        env = dict(os.environ)

        # First run — caches the script
        _run_command(cmd, env, 10, "heal-test", "id")
        script = scripts_dir / "heal-test.sh"
        assert script.exists()

        # Break the cached script
        _write_cached_script(script, cmd, "exit 1")

        # Run again — cached script fails (exit 1), falls back to direct,
        # and re-caches the working command
        result = _run_command(cmd, env, 10, "heal-test", "id")
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "healthy"

        # Verify the script was re-cached (self-healed)
        healed_content = script.read_text()
        assert "echo" in healed_content
        assert "healthy" in healed_content

    def test_tool_poll_caches_through_monitor_interface(self, scripts_dir):
        """tool_poll (the public check runner) caches scripts end-to-end."""
        payload = json.dumps([{"id": "msg-42", "from": "test@example.com"}])
        monitor = Monitor(
            name="integration-email",
            check="tool_poll",
            event="monitor/email.received",
            extra={"command": f"echo '{payload}'", "id_field": "id"},
        )

        result = tool_poll(monitor, [Path("/repo")])
        assert result is not None
        assert len(result) == 1
        assert result[0].key == "msg-42"
        assert result[0].data["from"] == "test@example.com"

        # Verify script was cached under the monitor name
        script = scripts_dir / "integration-email.sh"
        assert script.exists()

    def test_cache_invalidated_on_command_failure(self, scripts_dir):
        """When the direct command also fails, the stale cache is removed."""
        env = dict(os.environ)

        # Seed a cached script for this command that fails at runtime
        script = scripts_dir / "fail-test.sh"
        _write_cached_script(script, ["false"], "exit 1")

        # Run with a command that will also fail
        result = _run_command(["false"], env, 10, "fail-test", "id")
        assert result is None

        # Stale cache should be removed
        assert not script.exists()


# ===========================================================================
# script_cache runner (#327) — self-learning monitor runner
# ===========================================================================

import bobi.monitors.script_cache_checks as sc
from bobi.monitors.script_cache_checks import GenResult, script_cache


_SC_PUBLISHED: list = []

# Generation runs on a worker thread and a tick waits only GEN_HANDOFF_GRACE
# (2s) before detaching it. These lifecycle tests use a fake generator that
# returns in microseconds and assert on the COMPLETED outcome, so racing the
# real wall-clock grace would make them fail purely under CPU load. Give them a
# grace they cannot lose; the one test that exercises the handoff itself
# restores the production value.
REAL_GEN_HANDOFF_GRACE = sc.GEN_HANDOFF_GRACE
_SETTLED_GRACE = 60.0


@pytest.fixture()
def sc_scripts_dir(tmp_path):
    """Redirect the script_cache store + state into a temp dir and stub the
    real publish/install-policy wires (no network, no agent.yaml lookup)."""
    d = tmp_path / "sc_scripts"
    d.mkdir()
    _SC_PUBLISHED.clear()

    def fake_publish(event, data):
        _SC_PUBLISHED.append((event, data))
        return True

    with patch("bobi.monitors.script_cache_checks._scripts_dir", return_value=d), \
         patch("bobi.monitors.script_cache_checks.publish", side_effect=fake_publish), \
         patch("bobi.monitors.script_cache_checks._install_policy", return_value={}), \
         patch.object(sc, "GEN_HANDOFF_GRACE", _SETTLED_GRACE):
        yield d
    # An uncollected background generation must not leak into the next test.
    sc._generations.clear()


def _sc_monitor(name="unread-emails", **extra):
    base = {"prompt": "Check my email for unread messages", "id_field": "id"}
    base.update(extra)
    return Monitor(name=name, check="script_cache",
                   event="monitor/email.received", extra=base)


def _sc_gen(items, script):
    return lambda monitor, cwd, policy: GenResult(
        success=True, items=items, script=script, cost_usd=0.03)


def _sc_state(d, name="unread-emails"):
    p = d / f"{name}.state.json"
    return json.loads(p.read_text()) if p.exists() else {}


class TestScriptCacheRunnerIntegration:
    """End-to-end script_cache lifecycle with a real sandboxed subprocess."""

    def test_first_run_pins_then_second_run_is_cached_at_zero(self, sc_scripts_dir):
        candidate = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "echo '[{\"id\": \"msg-1\", \"subject\": \"hello\"}]'\n"
        )
        m = _sc_monitor()
        with patch.object(sc, "generate_candidate", _sc_gen([{"id": "msg-1"}], candidate)):
            r1 = script_cache(m, [Path("/repo")])
        assert r1 and r1[0].key == "msg-1"
        assert (sc_scripts_dir / "unread-emails.sc.sh").exists()

        # second run executes the cached script via a real sandboxed subprocess
        def boom(*a, **k):
            raise AssertionError("agent must not run on the cached path")
        with patch.object(sc, "generate_candidate", boom):
            r2 = script_cache(m, [Path("/repo")])
        assert r2 and r2[0].key == "msg-1"
        st = _sc_state(sc_scripts_dir)
        assert st["last_mode"] == "cached" and st["last_tick"]["cost_usd"] == 0.0

    def test_broken_active_script_self_heals_and_repins(self, sc_scripts_dir):
        good = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                "echo '[{\"id\": \"a\"}]'\n")
        m = _sc_monitor()
        with patch.object(sc, "generate_candidate", _sc_gen([{"id": "a"}], good)):
            script_cache(m, [Path("/repo")])
        # break the on-disk active script (also fails the TOCTOU integrity check)
        active = sc_scripts_dir / "unread-emails.sc.sh"
        active.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 7\n")
        healed = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                  "echo '[{\"id\": \"b\"}]'\n")
        with patch.object(sc, "generate_candidate", _sc_gen([{"id": "b"}], healed)):
            r = script_cache(m, [Path("/repo")])
        assert r and r[0].key == "b"
        # re-pinned: the new active content's hash matches trusted state
        assert sc._sha256(active.read_text()) == _sc_state(sc_scripts_dir)["sha256"]

    def test_scheduler_reconcile_semantics_match_tool_poll(self, tmp_path, sc_scripts_dir):
        """Full scheduler _reconcile: new IDs fire, same IDs dedup, disappeared
        drop, reappeared refire — identical to tool_poll."""
        from datetime import datetime, timezone
        from bobi.monitors.scheduler import MonitorScheduler

        monitor = _sc_monitor()
        fired: list = []

        class FakeRegistry:
            def effective_monitors(self):
                return [monitor]
            def projects_for(self, _m):
                return []

        sched = MonitorScheduler(
            publish=lambda event, data: (fired.append((event, data.get("id"))) or True),
            state_path=tmp_path / "monitor_state.json",
            now=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            registry_loader=lambda **kw: FakeRegistry(),
            spawn_check=lambda _m, _c, _cb: None,
        )
        # point the loaded check at our scripts dir + a controllable generator
        items_box = {"items": [{"id": "x"}, {"id": "y"}]}

        def gen(mon, cwd, policy):
            script = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                      "echo '" + json.dumps(items_box["items"]) + "'\n")
            return GenResult(True, items=items_box["items"], script=script, cost_usd=0.01)

        with patch.object(sc, "generate_candidate", gen):
            # tick 1: first sight of x, y → both fire
            sched.run_monitor(monitor, FakeRegistry(), sched._now())
            assert sorted(k for _, k in fired) == ["x", "y"]
            fired.clear()

            # tick 2: same IDs → cached path, dedup suppresses (nothing new fires)
            sched.run_monitor(monitor, FakeRegistry(), sched._now())
            assert fired == []

            # tick 3: y disappears, z appears → only z fires
            items_box["items"] = [{"id": "x"}, {"id": "z"}]
            sc.recache(monitor)  # config/data changed → regenerate
            sched.run_monitor(monitor, FakeRegistry(), sched._now())
            assert [k for _, k in fired] == ["z"]
            fired.clear()

            # tick 4: y reappears → refires (it had dropped out of active set)
            items_box["items"] = [{"id": "x"}, {"id": "y"}]
            sc.recache(monitor)
            sched.run_monitor(monitor, FakeRegistry(), sched._now())
            assert [k for _, k in fired] == ["y"]

    def test_slow_generation_does_not_stall_other_monitors(self, tmp_path,
                                                           scripts_dir,
                                                           sc_scripts_dir):
        """A script_cache generation runs out-of-band, so the single scheduler
        thread keeps evaluating every other monitor in the same tick (D004).

        Real scheduler, real tick, real tool_poll subprocess for the second
        monitor — only the agent runtime is stubbed (with a slow one).
        """
        import threading
        import time
        from datetime import datetime, timezone
        from bobi.monitors.scheduler import MonitorScheduler

        slow = _sc_monitor(name="slow-scriptcache")
        fast = Monitor(name="fast-poll", check="tool_poll",
                       event="monitor/fast",
                       extra={"command": 'echo \'[{"id": "fast-1"}]\'',
                              "id_field": "id"})
        fired: list = []

        class FakeRegistry:
            def effective_monitors(self):
                return [slow, fast]  # the blocker is evaluated first

            def projects_for(self, _m):
                return []

        sched = MonitorScheduler(
            publish=lambda event, data: (fired.append(event) or True),
            state_path=tmp_path / "monitor_state.json",
            now=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            registry_loader=lambda **kw: FakeRegistry(),
            spawn_check=lambda _m, _c, _cb: None,
        )

        candidate = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                     "echo '[{\"id\": \"slow-1\"}]'\n")

        release = threading.Event()

        def slow_gen(mon, cwd, policy):
            release.wait(30)  # a real generation takes minutes
            return GenResult(True, items=[{"id": "slow-1"}], script=candidate,
                             cost_usd=0.01)

        with patch.object(sc, "GEN_HANDOFF_GRACE", REAL_GEN_HANDOFF_GRACE), \
             patch.object(sc, "generate_candidate", slow_gen):
            t0 = time.monotonic()
            sched.tick()
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0, f"tick blocked for {elapsed:.1f}s"
            assert "monitor/fast" in fired, "the other monitor never ran"

            # The generation lands out-of-band; the next tick publishes it.
            release.set()
            assert sc._generations["slow-scriptcache"].done.wait(20)
            sched.run_monitor(slow, FakeRegistry(), sched._now())

        assert "monitor/email.received" in fired
        assert (sc_scripts_dir / "slow-scriptcache.sc.sh").exists()


@pytest.mark.skipif(
    not os.environ.get("BOBI_RUN_CLAUDE_TESTS"),
    reason="requires a real Claude CLI session (set BOBI_RUN_CLAUDE_TESTS=1)",
)
class TestScriptCacheRealAgent:
    """One real-agent test: an NL prompt generates → validates → pins an
    executable script end-to-end. Gated like the other Claude-CLI tests."""

    def test_nl_prompt_generates_validates_and_pins(self, sc_scripts_dir):
        m = _sc_monitor(
            name="real-gen",
            prompt="List the names of files in the current directory as a JSON "
                   "list of objects each with an 'id' field holding the filename. "
                   "Use only: ls is not allowed — use printf/echo with a literal.",
        )
        result = script_cache(m, [Path(".")])
        # the agent both produced this tick's items and a pinnable script
        assert result is not None
        if (sc_scripts_dir / "real-gen.sc.sh").exists():
            from bobi.monitors.script_cache_checks import validate_script
            assert validate_script((sc_scripts_dir / "real-gen.sc.sh").read_text()).ok
