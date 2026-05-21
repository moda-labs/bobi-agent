"""In-process event bus.

Simple queue that producers push to and the manager consumes from.
Thread-safe so webhook handlers and pollers can push concurrently.
"""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

EVENT_LOG = Path.home() / ".modastack" / "manager" / "events.jsonl"


class EventBus:
    def __init__(self, max_size: int = 1000):
        self._queue: deque[dict] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._event = threading.Event()

    def push(self, event_type: str, source: str, data: dict) -> None:
        """Push an event onto the bus."""
        event = {
            "type": event_type,
            "source": source,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data": data,
        }
        with self._lock:
            self._queue.append(event)
        self._event.set()

        # Append to event log
        try:
            EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(EVENT_LOG, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

        log.debug(f"Event: {event_type} from {source}")

    def drain(self) -> list[dict]:
        """Drain all pending events. Returns list of events (may be empty)."""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            self._event.clear()
        return events

    def wait(self, timeout: float = None) -> bool:
        """Block until an event arrives or timeout. Returns True if events pending."""
        return self._event.wait(timeout=timeout)

    def pending(self) -> int:
        """Number of events waiting."""
        return len(self._queue)


# Global singleton
_bus = EventBus()


def get_bus() -> EventBus:
    return _bus
