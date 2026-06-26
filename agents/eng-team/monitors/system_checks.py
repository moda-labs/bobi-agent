"""System health checks — disk usage monitoring.

Complements github_checks.py with host-level condition detection.
The disk_free check fires when usage exceeds a configurable threshold,
providing early warning before catastrophic disk-full events.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from bobi.monitors.schema import Condition


def disk_free(monitor, projects: list[Path]) -> list[Condition]:
    """Alert when disk usage exceeds a threshold (default: 85%).

    Checks the filesystem of each project path. The check is essentially
    free — shutil.disk_usage is a single statvfs syscall.
    """
    threshold_pct = int(monitor.extra.get("threshold_pct", 85))
    conditions: list[Condition] = []

    for project in projects:
        usage = shutil.disk_usage(str(project))
        used_pct = round(usage.used / usage.total * 100, 1)
        if used_pct >= threshold_pct:
            conditions.append(Condition(
                key=f"disk:{project}",
                data={
                    "path": str(project),
                    "used_pct": used_pct,
                    "free_gb": round(usage.free / (1024**3), 1),
                    "total_gb": round(usage.total / (1024**3), 1),
                },
            ))

    return conditions


CHECKS = {
    "disk_free": disk_free,
}
