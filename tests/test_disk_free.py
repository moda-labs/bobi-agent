"""Tests for the disk_free native monitor check.

Follows the same pattern as test_checks.py — imports the check module
from the agent pack and tests it with mocked system calls.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.monitors.schema import Condition


def _find_system_checks() -> Path:
    """Find system_checks.py from project-local agents."""
    repo_root = Path(__file__).parent.parent
    search_dirs = [
        repo_root / "agents",
        repo_root / ".modastack" / "agents",
    ]
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for pack in search_dir.iterdir():
            candidate = pack / "monitors" / "system_checks.py"
            if candidate.exists():
                return candidate
    return None


_checks_path = _find_system_checks()
if _checks_path is None:
    pytest.skip(
        "system_checks.py not found — create agents/eng-team/monitors/system_checks.py",
        allow_module_level=True,
    )

_spec = importlib.util.spec_from_file_location("modastack.monitors.system_checks", _checks_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["modastack.monitors.system_checks"] = _mod
_spec.loader.exec_module(_mod)

disk_free = _mod.disk_free
CHECKS = _mod.CHECKS


class TestDiskFree:

    def _mock_monitor(self, threshold_pct=85):
        m = MagicMock()
        m.extra = {"threshold_pct": threshold_pct}
        return m

    def test_alerts_when_above_threshold(self):
        # 90% used = 10% free on 100GB disk
        usage = MagicMock(total=100 * 1024**3, used=90 * 1024**3, free=10 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(), [Path("/tmp")])
            assert len(result) == 1
            assert result[0].data["used_pct"] == 90.0

    def test_no_alert_when_below_threshold(self):
        # 50% used
        usage = MagicMock(total=100 * 1024**3, used=50 * 1024**3, free=50 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(), [Path("/tmp")])
            assert result == []

    def test_custom_threshold(self):
        # 80% used, threshold at 75%
        usage = MagicMock(total=100 * 1024**3, used=80 * 1024**3, free=20 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(threshold_pct=75), [Path("/tmp")])
            assert len(result) == 1

    def test_exactly_at_threshold(self):
        # 85% used, threshold at 85%
        usage = MagicMock(total=100 * 1024**3, used=85 * 1024**3, free=15 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(threshold_pct=85), [Path("/tmp")])
            assert len(result) == 1

    def test_multiple_projects(self):
        usage = MagicMock(total=100 * 1024**3, used=95 * 1024**3, free=5 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(), [Path("/a"), Path("/b")])
            assert len(result) == 2

    def test_result_data_fields(self):
        usage = MagicMock(total=30 * 1024**3, used=27 * 1024**3, free=3 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            result = disk_free(self._mock_monitor(), [Path("/tmp")])
            data = result[0].data
            assert "used_pct" in data
            assert "free_gb" in data
            assert "total_gb" in data
            assert data["total_gb"] == 30.0


class TestSystemChecksRegistry:

    def test_registry_has_disk_free(self):
        assert "disk_free" in CHECKS
        assert callable(CHECKS["disk_free"])
