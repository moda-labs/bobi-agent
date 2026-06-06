"""Monitor definition: the small YAML record that describes one check."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Reserved keys parsed into named fields; everything else is free-form and
# kept in `extra` (e.g. `url:` for a deploy-health check) so new check types
# need no schema change.
_RESERVED = {"name", "description", "interval", "event", "check", "enabled"}

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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
    event: str = ""
    check: str = ""
    enabled: bool = True
    extra: dict = field(default_factory=dict)
    source: str = "user"
    project: str = ""

    @classmethod
    def from_dict(cls, raw: dict, source: str = "user", project: str = "") -> "Monitor":
        if not raw.get("name"):
            raise ValueError("Monitor record requires a 'name'")
        extra = {k: v for k, v in raw.items() if k not in _RESERVED}
        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            interval=str(raw.get("interval", "15m")),
            event=raw.get("event", f"monitor/{raw['name']}"),
            check=raw.get("check", ""),
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
        record["interval"] = self.interval
        if self.event:
            record["event"] = self.event
        if self.check:
            record["check"] = self.check
        if not self.enabled:
            record["enabled"] = False
        record.update(self.extra)
        return record

    @property
    def interval_seconds(self) -> int:
        return parse_interval(self.interval)

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
