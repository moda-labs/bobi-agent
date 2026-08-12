"""Expected-busy leases and worker-priority tests (MOD-364)."""

import json

import pytest

from bobi import paths
from bobi.expected_busy import (
    ExpectedBusyLease,
    active_lease_snapshot,
    acquire_lease,
    release_lease,
    renew_lease,
    run_command,
)
from bobi.worker_priority import lower_priority_command


def test_lease_is_bounded_renewable_and_released(tmp_path):
    root = tmp_path / "run"
    lease_id = acquire_lease(root, label="full unit gate", ttl=60, now=1000)

    snapshot = active_lease_snapshot(root, now=1001)
    assert snapshot == {
        "active": True,
        "lease_count": 1,
        "expires_at": 1060.0,
    }

    renew_lease(root, lease_id, ttl=60, now=1050)
    assert active_lease_snapshot(root, now=1061)["expires_at"] == 1110.0

    release_lease(root, lease_id)
    assert active_lease_snapshot(root, now=1062) is None


def test_expired_and_corrupt_lease_state_fails_open(tmp_path):
    root = tmp_path / "run"
    acquire_lease(root, label="old gate", ttl=10, now=1000)
    assert active_lease_snapshot(root, now=1030) is None

    lease_path = paths.expected_busy_path(root)
    lease_path.write_text("not json")
    assert active_lease_snapshot(root, now=1001) is None


def test_lease_ttl_is_bounded_and_rejects_non_finite_values(tmp_path):
    root = tmp_path / "run"
    low = acquire_lease(root, label="low", ttl=1, now=1000)
    high = acquire_lease(root, label="high", ttl=99999, now=1000)
    data = json.loads(paths.expected_busy_path(root).read_text())["leases"]
    assert data[low]["expires_at"] == 1030.0
    assert data[high]["expires_at"] == 4600.0
    with pytest.raises(ValueError, match="finite"):
        acquire_lease(root, label="bad", ttl=float("nan"), now=1000)


def test_multiple_commands_do_not_overwrite_each_others_lease(tmp_path):
    root = tmp_path / "run"
    first = acquire_lease(root, label="gate one", ttl=60, now=1000)
    second = acquire_lease(root, label="gate two", ttl=90, now=1001)

    snapshot = active_lease_snapshot(root, now=1002)
    assert snapshot == {
        "active": True,
        "lease_count": 2,
        "expires_at": 1091.0,
    }

    release_lease(root, first, now=1003)
    assert active_lease_snapshot(root, now=1003)["lease_count"] == 1
    release_lease(root, second, now=1003)
    assert active_lease_snapshot(root, now=1003) is None


def test_context_renews_until_exit_and_always_releases(tmp_path, monkeypatch):
    root = tmp_path / "run"
    lease = ExpectedBusyLease(root, label="suite", ttl=60)
    lease.__enter__()
    try:
        before = json.loads(paths.expected_busy_path(root).read_text())
        renewal_now = float(next(iter(before["leases"].values()))["expires_at"]) - 10
        monkeypatch.setattr("bobi.expected_busy.time.time", lambda: renewal_now)
        lease.renew()
        after = json.loads(paths.expected_busy_path(root).read_text())
        lease_id = lease.lease_id
        assert after["leases"][lease_id]["expires_at"] > \
            before["leases"][lease_id]["expires_at"]
    finally:
        lease.__exit__(None, None, None)

    assert active_lease_snapshot(root, now=3000) is None


def test_worker_priority_is_lowered_and_missing_nice_is_fail_open(monkeypatch):
    monkeypatch.setattr("bobi.worker_priority.shutil.which",
                        lambda name: "/usr/bin/nice")
    assert lower_priority_command(["python", "worker.py"]) == [
        "/usr/bin/nice", "-n", "5", "python", "worker.py",
    ]

    monkeypatch.setattr("bobi.worker_priority.shutil.which", lambda name: None)
    assert lower_priority_command(["python", "worker.py"]) == [
        "python", "worker.py",
    ]


def test_missing_wrapped_command_returns_shell_style_not_found(monkeypatch):
    monkeypatch.setattr(
        "bobi.expected_busy.subprocess.run",
        lambda command: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert run_command(("missing-command",)) == 127
