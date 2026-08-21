"""Wall-clock timestamps — one convention: timezone-aware UTC ISO-8601.

:func:`now_iso` is the standard writer and :func:`parse_iso` the standard
reader, so the persisted format has one definition. The fleet runs on boxes
and containers whose local timezone is not guaranteed, which is why the
convention is aware UTC and never local time. (The monitors' injectable
now-callables produce the same aware-UTC value; they stay datetime-valued
for arithmetic and test injection.)

Naive (offset-less) strings still on disk were written by older versions in
LOCAL time. :func:`parse_iso` defaults them to UTC — correct for every value
this convention wrote. A reader that must honor an old local-time value's
original meaning uses :func:`epoch_seconds`, whose naive branch exists for
exactly those legacy values.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_seconds(iso: str) -> float:
    """Epoch seconds from an ISO timestamp of either era, 0.0 when unparseable.

    An aware value reads as written. A naive value is a file written by an
    older version in the host's LOCAL time, so it is read as local — that is
    what its writer meant. This is deliberately NOT :func:`parse_iso`'s
    naive-means-UTC default, which is for values the current convention wrote.
    """
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


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
