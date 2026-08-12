"""CPU scheduling priority for delegated worker processes."""

from __future__ import annotations

import shutil

WORKER_NICE_INCREMENT = 5


def lower_priority_command(command: list[str], *,
                           increment: int = WORKER_NICE_INCREMENT) -> list[str]:
    """Prefix a worker command with ``nice`` when the host provides it."""
    nice = shutil.which("nice")
    if not nice:
        return list(command)
    return [nice, "-n", str(increment), *command]
