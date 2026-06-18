"""Integration test for tool_poll script caching lifecycle.

Exercises real file I/O and real subprocess calls (no mocks) to verify
the cache → run → fallback → self-heal lifecycle end-to-end.

Does NOT require Claude/LLM — tool_poll is a $0 native check runner.
"""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modastack.monitors.schema import Condition, Monitor
from modastack.monitors.scheduler import MonitorScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(tmp_path, monitors):
    """Build a scheduler with real tool_poll checks wired up."""
    published = []

    def _record(event, data):
        published.append({"event": event, "data": data})
        return True

    class FakeRegistry:
        def effective_monitors(self):
            return monitors

        def projects_for(self, m):
            return [tmp_path]

    sched = MonitorScheduler(
        publish=_record,
        state_path=tmp_path / "state.json",
        now=lambda: datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc),
        registry_loader=lambda: FakeRegistry(),
        spawn_check=lambda mon, cwd, on_verdict: None,
        project_path=tmp_path,
    )
    return sched, published


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestToolPollScriptCacheIntegration:
    """End-to-end script caching with real subprocess + real file I/O."""

    def test_cache_lifecycle(self, tmp_path, monkeypatch):
        """Full lifecycle: first run caches → second run uses cache →
        broken cache falls back and self-heals."""
        from modastack.monitors import tool_checks

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        monkeypatch.setattr(tool_checks, "_scripts_dir", lambda: scripts_dir)
        monkeypatch.setattr(
            tool_checks, "_script_path",
            lambda name: scripts_dir / f"{name.replace('/', '_').replace('..', '_')}.sh",
        )

        items = [{"id": "item-1", "val": "hello"}, {"id": "item-2", "val": "world"}]
        cmd_str = f"echo '{json.dumps(items)}'"

        monitor = Monitor(
            name="cache-test",
            check="tool_poll",
            event="monitor/cache-test",
            extra={"command": cmd_str, "id_field": "id"},
        )

        sched, published = _make_scheduler(tmp_path, [monitor])

        # --- Step 1: First run — command executes, script gets cached ---
        result = sched._check_conditions(monitor, sched._registry_loader())
        assert result is not None
        assert len(result) == 2
        assert {c.key for c in result} == {"item-1", "item-2"}

        script_path = scripts_dir / "cache-test.sh"
        assert script_path.exists(), "Script should be cached after first successful run"
        content = script_path.read_text()
        assert "echo" in content

        # --- Step 2: Mutate cached script to return different data ---
        new_items = [{"id": "cached-999", "val": "from-cache"}]
        script_path.write_text(
            f"#!/usr/bin/env bash\nset -euo pipefail\n"
            f"echo '{json.dumps(new_items)}'\n"
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

        result2 = sched._check_conditions(monitor, sched._registry_loader())
        assert result2 is not None
        assert len(result2) == 1
        assert result2[0].key == "cached-999", "Should use cached script output"

        # --- Step 3: Break the cached script → fallback + self-heal ---
        script_path.write_text("#!/usr/bin/env bash\nexit 1\n")
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

        result3 = sched._check_conditions(monitor, sched._registry_loader())
        assert result3 is not None
        assert len(result3) == 2, "Should fall back to direct execution"
        assert {c.key for c in result3} == {"item-1", "item-2"}

        # Script should be regenerated (self-healed)
        healed_content = script_path.read_text()
        assert "echo" in healed_content, "Cached script should be regenerated after fallback"

    def test_reconcile_with_cached_script(self, tmp_path, monkeypatch):
        """Conditions from a cached script flow through _reconcile correctly:
        new IDs fire events, same IDs dedup."""
        from modastack.monitors import tool_checks

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        monkeypatch.setattr(tool_checks, "_scripts_dir", lambda: scripts_dir)
        monkeypatch.setattr(
            tool_checks, "_script_path",
            lambda name: scripts_dir / f"{name.replace('/', '_').replace('..', '_')}.sh",
        )

        items = [{"id": "msg-1"}, {"id": "msg-2"}]
        monitor = Monitor(
            name="reconcile-cache-test",
            check="tool_poll",
            event="monitor/reconcile",
            extra={"command": f"echo '{json.dumps(items)}'", "id_field": "id"},
        )

        sched, published = _make_scheduler(tmp_path, [monitor])

        # First check + reconcile — fires events
        conds = sched._check_conditions(monitor, sched._registry_loader())
        assert conds is not None
        sched._reconcile(monitor, conds)
        assert len(published) == 2

        # Now the script is cached. Run again — same IDs should dedup.
        conds2 = sched._check_conditions(monitor, sched._registry_loader())
        assert conds2 is not None
        sched._reconcile(monitor, conds2)
        assert len(published) == 2, "Same IDs should not fire again"

        # Mutate cached script to add a new item
        new_items = [{"id": "msg-1"}, {"id": "msg-2"}, {"id": "msg-3"}]
        script_path = scripts_dir / "reconcile-cache-test.sh"
        script_path.write_text(
            f"#!/usr/bin/env bash\nset -euo pipefail\n"
            f"echo '{json.dumps(new_items)}'\n"
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

        conds3 = sched._check_conditions(monitor, sched._registry_loader())
        assert conds3 is not None
        sched._reconcile(monitor, conds3)
        assert len(published) == 3, "New item from cached script should fire"
        assert published[2]["data"]["monitor"] == "reconcile-cache-test"
