"""Track in-flight work between cron runs."""

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

from .config import STATE_PATH


class Status(Enum):
    DISPATCHED = "dispatched"
    WORKING = "working"
    AUDITING = "auditing"
    DONE = "done"
    FAILED = "failed"
    STUCK = "stuck"


@dataclass
class TrackedItem:
    """An item currently being worked on by an agent."""

    id: str
    status: Status
    repo_path: str
    title: str
    dispatched_at: float
    agent_pid: int | None = None
    pr_url: str | None = None
    branch: str | None = None
    last_checked: float = 0
    attempts: int = 1
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedItem":
        d["status"] = Status(d["status"])
        return cls(**d)


class StateStore:
    """JSON-file state store with compare-and-swap semantics."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._items: dict[str, TrackedItem] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._items = {}
            return
        raw = json.loads(self.path.read_text())
        self._items = {
            k: TrackedItem.from_dict(v) for k, v in raw.get("items", {}).items()
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"items": {k: v.to_dict() for k, v in self._items.items()}}
        # Atomic write to prevent corruption
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self.path)

    def is_tracked(self, item_id: str) -> bool:
        """Check if an item is already dispatched or in progress."""
        return item_id in self._items and self._items[item_id].status in (
            Status.DISPATCHED,
            Status.WORKING,
            Status.AUDITING,
        )

    def dispatch(self, item_id: str, repo_path: str, title: str, agent_pid: int | None = None, branch: str | None = None) -> bool:
        """Mark an item as dispatched. Returns False if already tracked (CAS)."""
        if self.is_tracked(item_id):
            return False

        self._items[item_id] = TrackedItem(
            id=item_id,
            status=Status.DISPATCHED,
            repo_path=repo_path,
            title=title,
            dispatched_at=time.time(),
            agent_pid=agent_pid,
            branch=branch,
        )
        self._save()
        return True

    def update_status(self, item_id: str, status: Status, **kwargs) -> None:
        """Update an item's status and optional fields."""
        if item_id not in self._items:
            return
        item = self._items[item_id]
        item.status = status
        item.last_checked = time.time()
        for k, v in kwargs.items():
            if hasattr(item, k):
                setattr(item, k, v)
        self._save()

    def get_in_flight(self) -> list[TrackedItem]:
        """Get all items currently being worked on."""
        return [
            item for item in self._items.values()
            if item.status in (Status.DISPATCHED, Status.WORKING, Status.AUDITING)
        ]

    def get_by_repo(self, repo_path: str) -> list[TrackedItem]:
        """Get in-flight items for a specific repo."""
        return [
            item for item in self.get_in_flight()
            if item.repo_path == repo_path
        ]

    def mark_done(self, item_id: str, pr_url: str | None = None) -> None:
        self.update_status(item_id, Status.DONE, pr_url=pr_url)

    def mark_failed(self, item_id: str, error: str) -> None:
        self.update_status(item_id, Status.FAILED, error=error)

    def mark_stuck(self, item_id: str) -> None:
        """Mark as stuck if running too long (>30 min for trivial, >2h for heavy)."""
        self.update_status(item_id, Status.STUCK)

    def cleanup_old(self, max_age_hours: int = 72) -> None:
        """Remove completed/failed items older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        self._items = {
            k: v for k, v in self._items.items()
            if v.status in (Status.DISPATCHED, Status.WORKING, Status.AUDITING)
            or v.dispatched_at > cutoff
        }
        self._save()
