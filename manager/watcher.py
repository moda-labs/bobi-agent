"""Fast event loop — polls channels for changes, wakes manager when needed.

Polls every N seconds (cheap — just API calls + tmux checks).
Only calls the manager (expensive — claude -p) when something changed.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from manager.context import gather_all, context_hash, write_context_prompt, MANAGER_DIR
from manager.loop import call_manager, DECISIONS_LOG
from manager.executor import execute_actions, post_thinking_placeholder

log = logging.getLogger(__name__)

LAST_HASH_PATH = MANAGER_DIR / "last_context_hash"


async def poll_and_maybe_think() -> dict | None:
    """Poll all channels. If anything changed, wake the manager."""
    context = await gather_all()
    current_hash = context_hash(context)

    last_hash = ""
    if LAST_HASH_PATH.exists():
        last_hash = LAST_HASH_PATH.read_text().strip()

    if current_hash == last_hash:
        return None

    log.info("Change detected — waking manager")

    LAST_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_HASH_PATH.write_text(current_hash)

    # Post "Thinking..." placeholder for any new Slack DMs
    slack_items = context.get("channels", {}).get("slack", [])
    for item in slack_items:
        if item.get("channel_id"):
            await post_thinking_placeholder(item["channel_id"])

    actions, reasoning = call_manager(context)
    summary = await execute_actions(actions) if actions else {"executed": 0, "skipped": 0, "errors": 0}

    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": context["timestamp"],
        "channels": {k: len(v) for k, v in context.get("channels", {}).items()},
        "reasoning": reasoning,
        "actions": actions,
        "outcome": summary,
    }
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if actions:
        log.info(f"Manager decided {len(actions)} actions: "
                 + ", ".join(a.get("type", "?") for a in actions))

    return summary


def run(poll_interval: int = 5):
    """Run the watcher loop."""
    log.info(f"Modabot starting. Polling every {poll_interval}s.")
    tick_count = 0
    last_tick = 0

    while True:
        try:
            result = asyncio.run(poll_and_maybe_think())
            if result is not None:
                last_tick = time.time()
                tick_count += 1
                if result.get("executed", 0) > 0 or result.get("errors", 0) > 0:
                    log.info(f"Tick #{tick_count}: {json.dumps(result)}")
            else:
                elapsed = int(time.time() - last_tick) if last_tick else 0
                if elapsed > 0 and elapsed % 60 < poll_interval:
                    log.info(f"Watching... ({elapsed}s since last tick)")
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
            logging.FileHandler(Path.home() / ".modastack" / "manager.log"),
        ],
    )

    interval = 5
    for arg in sys.argv[1:]:
        if arg.isdigit():
            interval = int(arg)

    run(interval)
