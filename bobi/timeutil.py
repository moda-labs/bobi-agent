"""Wall-clock timestamps — one convention: timezone-aware UTC ISO-8601.

Every wall-clock string bobi persists is written by :func:`now_iso` and read
back by :func:`parse_iso`, so the format has exactly one definition. The fleet
runs on boxes and containers whose local timezone is not guaranteed, which is
why the convention is aware UTC and never local time.

Naive (offset-less) strings still on disk were written by older versions in
LOCAL time. :func:`parse_iso` defaults them to UTC — correct for every value
this convention wrote; a reader that must honor an old local-time value's
original meaning handles the naive case itself (see
``bobi/webapp/runs.py:_epoch``).
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Read back a timestamp written by :func:`now_iso`, defaulting naive to UTC.

    Anything unparseable reads as absent — including a non-string pulled out
    of a JSON state document, which is why ``AttributeError`` is caught
    alongside ``ValueError``: state files in this repo are treated as empty
    when they do not parse, never as fatal.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
