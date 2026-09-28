"""Collector-owned maintenance requests for rebuild and retention operations."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bobi.fsutil import atomic_write_json, try_file_lock
from bobi.metrics.events import uuid7


class MaintenanceError(RuntimeError):
    pass


def prune_result_files(
    root: Path | str,
    *,
    keep: int = 256,
    max_age_days: int = 30,
    now: float | None = None,
) -> int:
    """Bound completed maintenance envelopes without touching pending requests."""
    request_root = Path(root).resolve() / "state" / "metrics" / "maintenance"
    files = sorted(
        request_root.glob("*.result.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    cutoff = (time.time() if now is None else now) - max_age_days * 86_400
    removed = 0
    for index, path in enumerate(files):
        if index < keep and path.stat().st_mtime >= cutoff:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _execute(root: Path, operation: str, options: dict[str, object]):
    if operation == "rebuild":
        from bobi.metrics.rebuild import rebuild_metrics

        return rebuild_metrics(root, _lock_held=True)
    if operation == "prune":
        from bobi.metrics.retention import apply_retention

        return apply_retention(
            root,
            retention_days=int(options.get("retention_days", 30)),
            _lock_held=True,
        )
    raise MaintenanceError(f"unsupported metrics maintenance operation: {operation}")


def process_pending_requests(root: Path | str) -> list[dict[str, object]]:
    """Execute queued operations while the caller owns the collector lock."""
    root = Path(root).resolve()
    request_root = root / "state" / "metrics" / "maintenance"
    prune_result_files(root)
    results = []
    for request_path in sorted(request_root.glob("*.request.json")):
        result_path = request_path.with_name(
            request_path.name.replace(".request.json", ".result.json")
        )
        try:
            request = json.loads(request_path.read_text())
            result = _execute(
                root,
                str(request["operation"]),
                dict(request.get("options") or {}),
            )
            envelope = {
                "status": "done",
                "request_id": request["request_id"],
                "operation": request["operation"],
                "completed_at_us": time.time_ns() // 1000,
                "result": result,
            }
        except Exception as exc:
            envelope = {
                "status": "error",
                "request_id": request_path.name.split(".", 1)[0],
                "operation": None,
                "completed_at_us": time.time_ns() // 1000,
                "error": type(exc).__name__,
                "message": str(exc),
            }
        atomic_write_json(result_path, envelope, sort_keys=True, fsync=True)
        request_path.unlink(missing_ok=True)
        results.append(envelope)
    prune_result_files(root)
    return results


def run_maintenance(
    root: Path | str,
    operation: str,
    *,
    options: dict[str, object] | None = None,
    wait: bool = False,
    timeout: float = 60.0,
) -> dict[str, object]:
    """Run directly when idle, otherwise queue for the active collector."""
    root = Path(root).resolve()
    metrics_root = root / "state" / "metrics"
    lock_path = metrics_root / "collector"
    with try_file_lock(lock_path) as acquired:
        if acquired:
            return {
                "status": "done",
                "operation": operation,
                "result": _execute(root, operation, options or {}),
            }
    request_id = str(uuid7())
    request_root = metrics_root / "maintenance"
    request_path = request_root / f"{request_id}.request.json"
    result_path = request_root / f"{request_id}.result.json"
    atomic_write_json(
        request_path,
        {
            "request_id": request_id,
            "operation": operation,
            "options": options or {},
            "requested_at_us": time.time_ns() // 1000,
        },
        sort_keys=True,
        fsync=True,
    )
    if not wait:
        return {
            "status": "pending",
            "request_id": request_id,
            "operation": operation,
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = json.loads(result_path.read_text())
        except (OSError, ValueError, TypeError):
            time.sleep(0.05)
            continue
        if result.get("status") == "error":
            raise MaintenanceError(str(result.get("message") or result.get("error")))
        return result
    raise MaintenanceError(
        f"metrics {operation} did not complete within {timeout:.1f}s; "
        f"request_id={request_id}"
    )
