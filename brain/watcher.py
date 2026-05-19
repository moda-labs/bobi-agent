"""Fast event loop that polls for changes and wakes the brain when needed.

Polls every 5 seconds (cheap — just Linear API + tmux checks).
Only calls the brain (expensive — claude -p) when something changed.
"""

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from brain.context import gather_all, write_context_prompt, BRAIN_DIR
from brain.loop import call_brain, DECISIONS_LOG
from brain.executor import execute_actions

log = logging.getLogger(__name__)

LAST_HASH_PATH = BRAIN_DIR / "last_context_hash"


def _context_hash(context: dict) -> str:
    """Hash the parts of context that matter for change detection."""
    relevant = {
        "issues": [(i["id"], i["state"]) for i in context["issues"]],
        "workers": [(w["issue_id"], w["session_state"], w["phase"])
                    for w in context["workers"]],
    }
    return hashlib.md5(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


async def poll_and_maybe_think() -> dict | None:
    """Poll for changes. If something changed, run the brain. Returns tick result or None."""
    context = await gather_all()
    current_hash = _context_hash(context)

    # Check if anything changed
    last_hash = ""
    if LAST_HASH_PATH.exists():
        last_hash = LAST_HASH_PATH.read_text().strip()

    if current_hash == last_hash:
        return None

    # Something changed — wake the brain
    log.info(f"Change detected — waking brain ({len(context['issues'])} issues, {len(context['workers'])} workers)")

    LAST_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_HASH_PATH.write_text(current_hash)

    actions, reasoning = call_brain(context)

    summary = await execute_actions(actions) if actions else {"executed": 0, "skipped": 0, "errors": 0}

    # Log decisions
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": context["timestamp"],
        "issues_seen": len(context["issues"]),
        "workers_seen": len(context["workers"]),
        "reasoning": reasoning,
        "actions": actions,
        "outcome": summary,
    }
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if actions:
        log.info(f"Brain decided {len(actions)} actions: "
                 + ", ".join(a.get("type", "?") for a in actions))

    return summary


def run(poll_interval: int = 5):
    """Run the watcher loop. Polls every N seconds, thinks only when needed."""
    log.info(f"Modabot watcher starting. Polling every {poll_interval}s, brain wakes on changes.")
    tick_count = 0
    last_brain_time = 0

    while True:
        try:
            result = asyncio.run(poll_and_maybe_think())
            if result is not None:
                last_brain_time = time.time()
                tick_count += 1
                if result.get("executed", 0) > 0 or result.get("errors", 0) > 0:
                    log.info(f"Brain tick #{tick_count}: {json.dumps(result)}")
            else:
                # Nothing changed — periodic heartbeat log
                elapsed = int(time.time() - last_brain_time) if last_brain_time else 0
                if elapsed > 0 and elapsed % 60 < poll_interval:
                    log.info(f"Watching... ({elapsed}s since last brain tick)")
        except Exception as e:
            log.error(f"Poll failed: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(Path.home() / ".dispatch" / "brain.log"),
        ],
    )

    interval = 5
    for arg in sys.argv[1:]:
        if arg.isdigit():
            interval = int(arg)

    run(interval)
