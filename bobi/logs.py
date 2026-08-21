"""One line shape for every log Bobi writes to disk.

Bobi's runtime logs are append-only files that outlive both the process and
the day: `state/manager.log` and `state/embedding-sidecar.log`. A wall-clock-
only stamp makes them ambiguous across a day boundary and actively misleading
during an incident - four once-a-day monitor fires sitting adjacent read as a
runaway loop firing four times in 30 seconds (#851). Every line therefore
carries a full ISO-8601 local timestamp with its UTC offset.

The other half of a readable append-only log is that each record appears
once. Bobi can hold two writers onto one log at the same time - stderr
redirected into the file it also attaches a handler to - and every doubled
line inflates exactly the counts an operator reads back out of an incident.
`root_writes_to` is how a writer asks whether it would be the second one.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def _iso(epoch: float) -> str:
    """*epoch* as ISO-8601 local time with offset, to the millisecond."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


class IsoFormatter(logging.Formatter):
    """`%(asctime)s` as `2026-07-25T15:00:19.417-07:00`, never a bare clock.

    The offset is part of the stamp because a fleet reads logs from hosts in
    more than one zone, and a retained file is often read long after the host
    that wrote it moved on. Milliseconds stay because ordering within a second
    is the other thing an incident asks of a log.

    The shape is fixed: `datefmt` exists for the base class's call contract
    and is ignored, so every Bobi sink emits the same prefix and one grep
    spans all of them.
    """

    def formatTime(self, record: logging.LogRecord,
                   datefmt: str | None = None) -> str:
        return _iso(record.created)


def formatter() -> logging.Formatter:
    """The house formatter: an ISO stamp, the level, then the message."""
    return IsoFormatter(LOG_FORMAT)


def configure_root(level: int = logging.INFO) -> None:
    """Send this process's logs to stderr in the house line format.

    `force` because plain `basicConfig` stands down when the root logger
    already has a handler, and something upstream often does: a `bobi.commands`
    plugin is imported while click is still resolving the command name, and any
    dependency that calls `basicConfig` at import time gets there first.
    Without it the house format is silently skipped, the level stays at
    WARNING, and the log keeps the very lines #851 is about.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(formatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def file_handler(path: Path) -> logging.FileHandler:
    """A `FileHandler` on *path* that writes the house line format."""
    handler = logging.FileHandler(path)
    handler.setFormatter(formatter())
    return handler


def stamped(level: str, message: str) -> str:
    """*message* as a log line matching every other line in the file.

    For the writers that append to a log directly instead of going through
    a `logging` handler; without it their lines are the ones with no date.
    """
    return f"{_iso(time.time())} [{level}] {message}"


def root_writes_to(path: Path) -> bool:
    """True when the root logger already puts records into the file at *path*.

    Two different handlers can land in the same log and Bobi uses both: a
    `StreamHandler` on a stderr that was redirected into the file (how
    `spawn_team` and `_spawn_monitor_agent` launch their children), and the
    `FileHandler` `_attach_runtime_log` opens on it directly. A second writer
    that checks for only one of them still doubles every record, so this asks
    the question by the file each handler's stream actually holds open - which
    answers for both at once.

    Anything unknowable - a handler with no stream, a stream with no fileno,
    a path that does not exist yet - counts as "not writing there". A
    duplicated line is recoverable; a dropped one is gone.
    """
    try:
        target = path.stat()
    except OSError:
        return False
    for handler in list(logging.getLogger().handlers):
        stream = getattr(handler, "stream", None)
        if stream is None:
            continue
        try:
            if os.path.samestat(os.fstat(stream.fileno()), target):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False
