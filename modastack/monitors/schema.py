"""Monitor definition and condition types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Condition:
    """One detected condition: a dedup key plus the event payload."""

    key: str
    data: dict = field(default_factory=dict)

# Reserved keys parsed into named fields; everything else is free-form and
# kept in `extra` (e.g. `url:` for a deploy-health check) so new check types
# need no schema change.
_RESERVED = {"name", "description", "interval", "event", "check", "command",
             "enabled", "at", "tz", "days", "notify", "role", "curator"}

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Weekday names -> Python weekday() integer (Monday=0 … Sunday=6).
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

# Numeric weekdays -> Python weekday(). Both common conventions are accepted:
# cron-style (0=Sunday … 6=Saturday) and ISO (7=Sunday). 1–6 mean Mon–Sat in
# both, so they're unambiguous; only Sunday differs (0 vs 7) and both map here.
_WEEKDAY_NUMBERS = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}


def parse_interval(value: str | int) -> int:
    """Parse an interval like '15m', '1h', '2d', '30s' into seconds.

    A bare number is treated as seconds. Raises ValueError on garbage.
    """
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        text = str(value).strip().lower()
        match = re.fullmatch(r"(\d+)\s*([smhd]?)", text)
        if not match:
            raise ValueError(f"Invalid interval: {value!r}")
        amount = int(match.group(1))
        unit = match.group(2) or "s"
        seconds = amount * _UNIT_SECONDS[unit]
    if seconds <= 0:
        raise ValueError(f"Interval must be positive: {value!r}")
    return seconds


def parse_at(value) -> list[tuple[int, int]]:
    """Parse `at:` times — '06:00' or ['06:00', '18:00'] — into (hour, minute)
    tuples. Interpreted in the monitor's `tz` (IANA name), falling back to
    the host's local timezone. Raises ValueError on garbage.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    times = []
    for item in items:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(item).strip())
        if not match:
            raise ValueError(f"Invalid at-time: {item!r} (expected 'HH:MM')")
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"Invalid at-time: {item!r} (expected 'HH:MM')")
        times.append((hour, minute))
    return times


def parse_days(value) -> set[int]:
    """Parse `days:` — weekday names and/or numbers — into a set of Python
    weekday integers (Monday=0 … Sunday=6, matching ``datetime.weekday()``).

    Accepts 3-letter or full names (case-insensitive: ``sun``, ``Sunday``) and
    numbers in either common convention: cron-style (``0``=Sunday … ``6``=Saturday)
    and ISO (``7``=Sunday). ``1``–``6`` mean Mon–Sat in both. A single value or a
    list is accepted; an empty/absent value means *every day* (no weekday
    gating). Raises ValueError on anything unrecognized.

    `days:` only has meaning alongside `at:` — it gates which weekdays the
    wall-clock `at:` times are eligible to fire on.
    """
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    days: set[int] = set()
    for item in items:
        token = str(item).strip().lower()
        if not token:
            continue
        if token.lstrip("+").isdigit():
            num = int(token)
            if num not in _WEEKDAY_NUMBERS:
                raise ValueError(f"Invalid weekday number: {item!r} (use 0–7)")
            days.add(_WEEKDAY_NUMBERS[num])
        elif token in _WEEKDAY_NAMES:
            days.add(_WEEKDAY_NAMES[token])
        else:
            raise ValueError(f"Invalid weekday: {item!r} "
                             "(use names like 'sun' or numbers 0–7)")
    return days


@dataclass
class Monitor:
    """One monitoring task, resolved from a single YAML record.

    `source` is the tier it came from ("default", "user", or a project path).
    `project` is set only for project-specific monitors — it scopes the check to
    that project.
    """

    name: str
    description: str = ""
    interval: str = "15m"
    at: list = field(default_factory=list)
    tz: str = ""
    days: list = field(default_factory=list)
    event: str = ""
    check: str = ""
    command: str = ""
    notify: bool = False
    role: str = ""
    curator: bool = False
    enabled: bool = True
    extra: dict = field(default_factory=dict)
    source: str = "user"
    project: str = ""

    @classmethod
    def from_dict(cls, raw: dict, source: str = "user", project: str = "") -> "Monitor":
        if not raw.get("name"):
            raise ValueError("Monitor record requires a 'name'")
        extra = {k: v for k, v in raw.items() if k not in _RESERVED}
        at = raw.get("at")
        days = raw.get("days")
        if days is None or days == [] or days == "":
            days = []  # absent/empty = every day (no weekday gating)
        elif not isinstance(days, list):
            days = [days]  # tolerate a bare scalar (`days: sun` / `days: 0`)
        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            interval=str(raw.get("interval", "15m")),
            at=(at if isinstance(at, list) else [at]) if at else [],
            tz=raw.get("tz", ""),
            days=days,
            event=raw.get("event", f"monitor/{raw['name']}"),
            check=raw.get("check", ""),
            command=raw.get("command", ""),
            notify=bool(raw.get("notify", False)),
            role=raw.get("role", ""),
            curator=bool(raw.get("curator", False)),
            enabled=raw.get("enabled", True),
            extra=extra,
            source=source,
            project=project,
        )

    def to_dict(self) -> dict:
        """Serialize back to a YAML record (only non-default fields)."""
        record: dict = {"name": self.name}
        if self.description:
            record["description"] = self.description
        if self.at:
            record["at"] = list(self.at)
            if self.tz:
                record["tz"] = self.tz
            if self.days:
                record["days"] = list(self.days)
        else:
            record["interval"] = self.interval
        if self.event:
            record["event"] = self.event
        if self.check:
            record["check"] = self.check
        if self.command:
            record["command"] = self.command
        if self.notify:
            record["notify"] = True
        if self.role:
            record["role"] = self.role
        if self.curator:
            record["curator"] = True
        if not self.enabled:
            record["enabled"] = False
        record.update(self.extra)
        return record

    @property
    def interval_seconds(self) -> int:
        return parse_interval(self.interval)

    @property
    def at_times(self) -> list[tuple[int, int]]:
        """Parsed (hour, minute) tuples from `at`, in the monitor's timezone."""
        return parse_at(self.at)

    @property
    def weekdays(self) -> set[int]:
        """Weekdays this monitor's `at:` is gated to, as Python weekday ints
        (Monday=0 … Sunday=6). Empty set = every day (no gating)."""
        return parse_days(self.days)

    @property
    def tzinfo(self):
        """The tzinfo for `at` times — the monitor's `tz` (IANA name) if set
        and resolvable, else the host's local timezone (None means local to
        datetime.astimezone)."""
        if self.tz:
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(self.tz)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"Monitor {self.name}: unknown tz {self.tz!r} — using host local time")
        return None

    @property
    def state_key(self) -> str:
        """Unique key for scheduler state — namespaced for project-scoped monitors
        so a global and a project-specific monitor of the same name don't collide.
        """
        return f"{self.name}@{self.project}" if self.project else self.name

    @property
    def event_parts(self) -> tuple[str, str]:
        """Split the event into (source, type) on the first '/'.

        'monitor/pr.conflict_detected' -> ('monitor', 'pr.conflict_detected'),
        so the manager sees a clean 'Event: monitor/pr.conflict_detected'.
        """
        event = self.event or f"monitor/{self.name}"
        if "/" in event:
            head, tail = event.split("/", 1)
            return head, tail
        return "monitor", event
