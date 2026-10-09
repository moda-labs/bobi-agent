"""Disposable-smoke-only fault injection for crash-recovery verification."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from bobi.fsutil import atomic_write_json, try_file_lock

FAULT_ENV = "BOBI_METRICS_FAULT_INJECTION"
SMOKE_MARKER = ".bobi-metrics-smoke.json"
DROP_NEXT_ONLINE_USAGE = "drop-next-online-usage"


class FaultInjectionRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class FaultAction:
    fault: str
    turn_id: str
    hold_seconds: float


def _smoke_home(root: Path) -> Path | None:
    for candidate in (root, *root.parents):
        if (candidate / SMOKE_MARKER).is_file():
            return candidate
    return None


def fault_injection_enabled(root: Path | str) -> bool:
    if os.environ.get(FAULT_ENV, "").strip() != "1":
        return False
    return _smoke_home(Path(root).resolve()) is not None


def validate_fault_injection(root: Path | str) -> None:
    if os.environ.get(FAULT_ENV, "").strip() != "1":
        return
    if _smoke_home(Path(root).resolve()) is None:
        raise FaultInjectionRefused(
            f"{FAULT_ENV}=1 is allowed only in a disposable metrics smoke home"
        )


def fault_path(root: Path | str) -> Path:
    return Path(root).resolve() / "state" / "metrics" / "fault.json"


def arm_fault(
    root: Path | str,
    fault: str,
    *,
    hold_seconds: float = 60.0,
) -> dict[str, object]:
    resolved_root = Path(root).resolve()
    if _smoke_home(resolved_root) is None:
        raise FaultInjectionRefused(
            "metrics faults can be armed only in a disposable metrics smoke home"
        )
    validate_fault_injection(resolved_root)
    if fault != DROP_NEXT_ONLINE_USAGE:
        raise FaultInjectionRefused(f"unsupported metrics fault: {fault}")
    state = {
        "fault": fault,
        "status": "armed",
        "count": 1,
        "hold_seconds": max(0.0, float(hold_seconds)),
        "armed_at_us": time.time_ns() // 1000,
        "turn_id": None,
    }
    atomic_write_json(fault_path(resolved_root), state, sort_keys=True, fsync=True)
    return state


def read_fault(root: Path | str) -> dict[str, object] | None:
    try:
        data = json.loads(fault_path(root).read_text())
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def consume_drop_online_usage(
    root: Path | str,
    turn_id: str,
) -> FaultAction | None:
    if os.environ.get(FAULT_ENV, "").strip() != "1":
        return None
    validate_fault_injection(root)
    path = fault_path(root)
    with try_file_lock(path) as acquired:
        if not acquired:
            return None
        state = read_fault(root)
        if not state or state.get("status") != "armed":
            return None
        if state.get("fault") != DROP_NEXT_ONLINE_USAGE:
            return None
        state.update({
            "status": "consumed",
            "count": 0,
            "turn_id": turn_id,
            "consumed_at_us": time.time_ns() // 1000,
        })
        atomic_write_json(path, state, sort_keys=True, fsync=True)
        return FaultAction(
            fault=DROP_NEXT_ONLINE_USAGE,
            turn_id=turn_id,
            hold_seconds=float(state.get("hold_seconds") or 0.0),
        )
