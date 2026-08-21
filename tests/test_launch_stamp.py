"""Running-vs-installed drift for long-lived local processes (#928).

An in-place `uv tool install --upgrade` replaces bobi underneath the manager
and the local event server, which keep serving code that no longer exists on
disk. These tests pin the detection: what each process recorded at launch,
compared against what is installed now.
"""

import json
import os
import subprocess
import sys

import pytest

from bobi import launch_stamp, paths
from bobi.launch_stamp import (
    EVENT_SERVER,
    MANAGER,
    inspect_processes,
    installed_bobi_version,
    record_launch,
    stale_processes,
)


@pytest.fixture
def root(tmp_path):
    """A runtime root with the layout the stamps live in."""
    paths.state_path(tmp_path).mkdir(parents=True)
    (tmp_path / "package").mkdir()
    return tmp_path


@pytest.fixture
def live_pid():
    """A real, live process to point a pid file at."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _dead_pid() -> int:
    """A pid that has certainly exited."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait(timeout=10)
    return proc.pid


def _write_pid(root, component: str, pid: int) -> None:
    launch_stamp.pid_path(root, component).write_text(str(pid))


def _older_version() -> str:
    """A version string that is never the one installed under the test."""
    return installed_bobi_version() + "-older"


def _bundle(root, body: bytes = b"console.log('v1')\n"):
    """A stand-in for the event server's `dist/local.js` bundle."""
    bundle = root / "event-server" / "dist" / "local.js"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(body)
    return bundle


# --- A clean install, no upgrade: no false positives (AC3) ---


class TestCleanInstall:
    def test_processes_launched_from_the_installed_bobi_are_not_stale(
        self, root, live_pid,
    ):
        _write_pid(root, MANAGER, live_pid)
        _write_pid(root, EVENT_SERVER, live_pid)
        record_launch(root, MANAGER, live_pid)
        record_launch(root, EVENT_SERVER, live_pid, artifact=_bundle(root))

        assert stale_processes(root) == []
        assert {p.name for p in inspect_processes(root)} == {
            "manager", "event server"}

    def test_nothing_running_reports_nothing(self, root):
        dead = _dead_pid()
        _write_pid(root, MANAGER, dead)
        _write_pid(root, EVENT_SERVER, dead)
        record_launch(root, MANAGER, dead)

        assert inspect_processes(root) == []

    def test_no_pid_files_reports_nothing(self, root):
        assert inspect_processes(root) == []

    def test_a_stamp_left_by_a_dead_process_is_ignored(self, root):
        """The pid file is gone, so the leftover stamp says nothing."""
        record_launch(root, MANAGER, _dead_pid())

        assert inspect_processes(root) == []


# --- An in-place upgrade leaves processes on replaced code (AC1) ---


class TestUpgradeDrift:
    def test_version_mismatch_names_each_stale_process(self, root, live_pid):
        _write_pid(root, MANAGER, live_pid)
        _write_pid(root, EVENT_SERVER, live_pid)
        for component in (MANAGER, EVENT_SERVER):
            record_launch(root, component, live_pid)
            stamp = json.loads(launch_stamp.stamp_path(root, component).read_text())
            stamp["bobi_version"] = _older_version()
            launch_stamp.stamp_path(root, component).write_text(json.dumps(stamp))

        stale = stale_processes(root)

        assert [p.name for p in stale] == ["manager", "event server"]
        for process in stale:
            assert process.pid == live_pid
            assert _older_version() in process.detail
            assert installed_bobi_version() in process.detail

    def test_a_process_with_no_stamp_is_stale(self, root, live_pid):
        """A live process no stamp accounts for predates launch stamping.

        This is the observed case: the running event server was started by a
        bobi that never recorded anything, so the only honest report is that
        its launch version is unknown.
        """
        _write_pid(root, EVENT_SERVER, live_pid)

        (stale,) = stale_processes(root)

        assert stale.name == "event server"
        assert stale.pid == live_pid
        assert "unknown" in stale.detail

    def test_a_stamp_for_a_different_pid_is_not_evidence(self, root, live_pid):
        """A stamp from an earlier process says nothing about this one."""
        record_launch(root, EVENT_SERVER, live_pid + 100000)
        _write_pid(root, EVENT_SERVER, live_pid)

        (stale,) = stale_processes(root)

        assert stale.pid == live_pid
        assert "unknown" in stale.detail

    def test_a_replaced_artifact_is_stale_at_the_same_version(self, root, live_pid):
        """The upgrade overwrote `dist/local.js` under the running node."""
        bundle = _bundle(root)
        _write_pid(root, EVENT_SERVER, live_pid)
        record_launch(root, EVENT_SERVER, live_pid, artifact=bundle)
        bundle.write_bytes(b"console.log('v2 - replaced by the upgrade')\n")

        (stale,) = stale_processes(root)

        assert stale.name == "event server"
        assert "local.js" in stale.detail
        assert "replaced" in stale.detail

    def test_an_untouched_artifact_is_not_stale(self, root, live_pid):
        bundle = _bundle(root)
        _write_pid(root, EVENT_SERVER, live_pid)
        record_launch(root, EVENT_SERVER, live_pid, artifact=bundle)
        os.utime(bundle, (0, 0))  # mtime alone must not decide staleness

        assert stale_processes(root) == []

    def test_a_deleted_artifact_is_stale(self, root, live_pid):
        bundle = _bundle(root)
        _write_pid(root, EVENT_SERVER, live_pid)
        record_launch(root, EVENT_SERVER, live_pid, artifact=bundle)
        bundle.unlink()

        (stale,) = stale_processes(root)

        assert "local.js" in stale.detail

    def test_the_remedy_names_the_command_that_replaces_the_process(
        self, root, live_pid,
    ):
        _write_pid(root, MANAGER, live_pid)
        _write_pid(root, EVENT_SERVER, live_pid)

        remedies = {p.name: p.remedy for p in stale_processes(root)}

        agent = paths.agent_name_for_root(root)
        assert remedies["manager"] == f"bobi agent {agent} restart"
        assert remedies["event server"] == f"bobi agent {agent} event-server restart"


# --- Restarting the named processes clears the check (AC2) ---


class TestRestartClears:
    def test_recording_a_fresh_launch_clears_the_finding(self, root, live_pid):
        bundle = _bundle(root)
        _write_pid(root, EVENT_SERVER, live_pid)
        assert stale_processes(root)  # unstamped: the pre-fix process

        record_launch(root, EVENT_SERVER, live_pid, artifact=bundle)

        assert stale_processes(root) == []

    def test_clearing_a_launch_stamp_leaves_no_stale_stamp_behind(
        self, root, live_pid,
    ):
        record_launch(root, MANAGER, live_pid)
        launch_stamp.clear_launch(root, MANAGER, pid=live_pid)

        assert not launch_stamp.stamp_path(root, MANAGER).exists()

    def test_clearing_does_not_remove_another_process_stamp(self, root, live_pid):
        """A losing shutdown race must not delete the live process's stamp."""
        record_launch(root, MANAGER, live_pid)

        launch_stamp.clear_launch(root, MANAGER, pid=live_pid + 100000)

        assert launch_stamp.stamp_path(root, MANAGER).exists()


# --- Container deployments cannot drift (AC4) ---


class TestContainerDeployment:
    def test_image_started_processes_report_nothing(self, root, live_pid,
                                                    monkeypatch):
        """The image is the unit of update, so nothing outlives its code.

        A container's processes are always started from the image's bobi, so
        the recorded and installed versions are the same one and the check has
        nothing to say - no platform special-case required.
        """
        monkeypatch.setenv("FLY_APP_NAME", "bobi-fleet-instance")
        monkeypatch.setenv("BOBI_IMAGE", "ghcr.io/moda-labs/bobi:latest")
        _write_pid(root, MANAGER, live_pid)
        _write_pid(root, EVENT_SERVER, live_pid)
        record_launch(root, MANAGER, live_pid)
        record_launch(root, EVENT_SERVER, live_pid, artifact=_bundle(root))

        assert stale_processes(root) == []


# --- Recording is never allowed to break a start ---


class TestRecordingIsBestEffort:
    def test_an_unwritable_state_dir_does_not_raise(self, root, live_pid):
        def boom(*args, **kwargs):
            raise OSError("read-only file system")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(launch_stamp, "atomic_write_json", boom)
            record_launch(root, MANAGER, live_pid)

        assert not launch_stamp.stamp_path(root, MANAGER).exists()

    def test_an_unreadable_stamp_reads_as_no_stamp(self, root, live_pid):
        _write_pid(root, MANAGER, live_pid)
        launch_stamp.stamp_path(root, MANAGER).write_text("{not json")

        (stale,) = stale_processes(root)

        assert "unknown" in stale.detail
