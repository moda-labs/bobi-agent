"""Durable state survives a kill mid-write (Phase 3: D034, D038, D087, Q039).

Every test here kills a real child process partway through a write and then
asserts the previously-persisted document is still readable. Each one fails
against the pre-fix bare ``write_text`` and passes once the writer goes
through :mod:`bobi.fsutil`.

The kill is injected by replacing ``Path.write_text`` with a version that
writes half the bytes and then SIGKILLs itself. That hook is deliberately
writer-agnostic — it lands on the target file for a non-atomic writer and on
the temp sibling for an atomic one, which is exactly the difference under
test.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_HALF_WRITE_KILL = '''
import os, signal
from pathlib import Path

_real_write_text = Path.write_text

def _half_then_die(self, data, *args, **kwargs):
    _real_write_text(self, data[: max(1, len(data) // 2)])
    os.kill(os.getpid(), signal.SIGKILL)

def arm():
    Path.write_text = _half_then_die
'''


def _run_child(body: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    # Each piece is dedented on its own: interpolating an unindented block
    # into an indented f-string leaves dedent() with no common prefix to
    # strip, which silently produces a child that dies on IndentationError
    # before it ever reaches the write under test.
    script = (
        f"import sys\nsys.path.insert(0, {REPO_ROOT!r})\n"
        + _HALF_WRITE_KILL
        + textwrap.dedent(body)
    )
    env = dict(os.environ)
    env.pop("BOBI_HOME", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, env=env, timeout=120
    )


def _assert_killed(proc: subprocess.CompletedProcess) -> None:
    """The child must have died in the write window, not exited normally.

    Without this, a child that failed to import or never reached the write
    would leave the target untouched and the test would pass vacuously.
    """
    assert proc.returncode == -9, (
        f"child did not reach the kill point: rc={proc.returncode}\n"
        f"stdout={proc.stdout.decode()[-3000:]}\n"
        f"stderr={proc.stderr.decode()[-3000:]}"
    )


# --- D034: bobi/setup/state.py -------------------------------------------

class TestSetupStateSurvivesKill:
    def test_kill_mid_save_keeps_the_resumable_document(self, tmp_path):
        """A killed save must not discard an in-progress setup.

        Pre-fix, save() truncated setup.json in place, load() hit
        JSONDecodeError and returned None, and `bobi setup --resume` printed
        'No setup in progress' — the whole wizard session gone.
        """
        home = tmp_path / "home"
        project = tmp_path / "proj"
        proc = _run_child(
            f"""
            from pathlib import Path
            from bobi.setup.state import SetupState, Stage

            project = Path({str(project)!r})
            state = SetupState()
            state.spec.goal = "ORIGINAL GOAL THAT MUST SURVIVE"
            state.stage = Stage.DESIGN
            state.save(project)

            arm()
            state.spec.goal = "half-written replacement"
            state.save(project)
            """,
            {"BOBI_HOME": str(home)},
        )
        _assert_killed(proc)

        from bobi.setup.state import SetupState

        loaded = SetupState.load(project)
        assert loaded is not None, "setup.json was corrupted by the killed save"
        assert loaded.spec.goal == "ORIGINAL GOAL THAT MUST SURVIVE"

    def test_save_serializes_across_processes(self, tmp_path):
        """`bobi setup` and `bobi app` can drive the same project at once.

        Both construct their own SetupState over one setup.json, so the
        publish has to take a cross-process lock, not just be atomic.
        """
        import time

        from bobi.fsutil import file_lock
        from bobi.setup.state import SetupState, setup_state_file

        os.environ["BOBI_HOME"] = str(tmp_path / "home")
        project = tmp_path / "proj"
        SetupState().save(project)          # materialize the path
        target = setup_state_file(project)

        child = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {REPO_ROOT!r})
            import os
            os.environ["BOBI_HOME"] = {str(tmp_path / "home")!r}
            from pathlib import Path
            from bobi.setup.state import SetupState
            start = time.monotonic()
            SetupState().save(Path({str(project)!r}))
            print(time.monotonic() - start)
            """
        )
        # Popen, not run(): the child is supposed to BLOCK on the lock this
        # process holds, so waiting for it inside the `with` would deadlock.
        with file_lock(target):
            proc = subprocess.Popen([sys.executable, "-c", child],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.6)
            assert proc.poll() is None, "child's save() completed while the lock was held"
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()[-2000:]
        # It only got through once this process released — so save() really
        # waited on the lock rather than publishing over a concurrent writer.
        waited = float(out.decode().strip())
        assert waited >= 0.4, f"save() did not block on the lock (took {waited:.3f}s)"


    def test_orphan_temp_from_a_killed_save_does_not_change_the_source_hash(
            self, tmp_path):
        """A crash leaves a temp sibling; it must not read as a source change.

        The helper cleans up on every exit it controls, but not a SIGKILL —
        so a save killed mid-write leaves `.setup.json.<pid>.<ns>.tmp`
        behind. When run/ sits inside the team source tree that orphan lands
        under source_tree_hash, and counting it fails the next install with
        "the team source changed since validate_team last passed". Its name
        is process-unique, so no exclude list can name it in advance.
        """
        from bobi.setup.state import source_tree_hash

        pack = tmp_path / "pack"
        (pack / "state").mkdir(parents=True)
        (pack / "agent.yaml").write_text("agent: my-team\n")
        before = source_tree_hash(pack)

        orphan = pack / "state" / ".setup.json.4242.1785000000000000000.tmp"
        orphan.write_text('{"half-writ')
        assert source_tree_hash(pack) == before

        # A real (non-temp) new file still changes it — the skip is narrow.
        (pack / "state" / "real.json").write_text("{}")
        assert source_tree_hash(pack) != before


# --- D038: bobi/brain/codex_config.py ------------------------------------

class TestCodexConfigSurvivesKill:
    def test_kill_mid_write_keeps_foreign_settings(self, tmp_path):
        """config.toml carries operator-owned keys bobi must never lose."""
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        foreign = 'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "xhigh"\n'
        config.write_text(foreign)

        proc = _run_child(
            f"""
            from pathlib import Path
            from bobi.brain.codex_config import write_codex_config

            arm()
            write_codex_config(
                {{"linear": {{"command": "npx", "args": ["-y", "linear-mcp"]}}}},
                home=Path({str(codex_home)!r}),
            )
            """
        )
        _assert_killed(proc)
        assert config.read_text() == foreign, "foreign codex settings were truncated"


# --- D087: bobi/spend_governor.py ----------------------------------------

class TestSpendGovernorConcurrency:
    def test_concurrent_recorders_do_not_lose_invocations(self, tmp_path):
        """Two processes recording at once must both land.

        Unlocked, this is read→prune→append→write: the second writer
        overwrites the first, one launch vanishes from the runaway-loop
        backstop, and the rolling cap undercounts.
        """
        from bobi.spend_governor import _load_state, _state_path

        project = tmp_path / "proj"
        (project / "state").mkdir(parents=True)

        child = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {REPO_ROOT!r})
            from pathlib import Path
            from bobi.spend_governor import record_invocation
            for _ in range(25):
                record_invocation(Path({str(project)!r}))
            """
        )
        procs = [subprocess.Popen([sys.executable, "-c", child],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(4)]
        for p in procs:
            assert p.wait(timeout=120) == 0, p.stderr.read().decode()[-2000:]

        recorded = _load_state(_state_path(project))
        assert len(recorded) == 100, (
            f"lost {100 - len(recorded)} invocations to concurrent writers"
        )

    def test_kill_mid_write_keeps_the_spend_window(self, tmp_path):
        """A torn spend file reads back as [] — the cap forgets everything."""
        from bobi.spend_governor import _load_state, _state_path, record_invocation

        project = tmp_path / "proj"
        (project / "state").mkdir(parents=True)
        for _ in range(3):
            record_invocation(project)
        before = _load_state(_state_path(project))
        assert len(before) == 3

        proc = _run_child(
            f"""
            from pathlib import Path
            from bobi.spend_governor import record_invocation
            arm()
            record_invocation(Path({str(project)!r}))
            """
        )
        _assert_killed(proc)
        after = _load_state(_state_path(project))
        assert after == before, "the rolling spend window was reset by a torn write"


# --- Q039: bobi/monitors/scheduler.py ------------------------------------

class TestMonitorSchedulerStateSurvivesKill:
    def test_kill_mid_save_keeps_last_run_times(self, tmp_path):
        """Corrupt monitor state is 'reset' on load — every monitor re-fires."""
        state_file = tmp_path / "monitor-state.json"
        original = {"nightly": {"last_run": 1754300000.0},
                    "hourly": {"last_run": 1754303600.0}}
        state_file.write_text(json.dumps(original, indent=2))

        proc = _run_child(
            f"""
            from pathlib import Path
            from bobi.monitors.scheduler import MonitorScheduler

            sched = MonitorScheduler.__new__(MonitorScheduler)
            sched.state_path = Path({str(state_file)!r})
            sched.state = {{"nightly": {{"last_run": 9999999999.0}}}}
            arm()
            sched._save_state()
            """
        )
        _assert_killed(proc)
        assert json.loads(state_file.read_text()) == original


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
