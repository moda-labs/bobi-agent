"""Modabot manager loop.

Every tick:
1. Gather context (Linear, workers, memory)
2. Call claude -p with the manager prompt + context
3. Parse JSON actions from the response
4. Execute actions

Stateless between ticks — all state lives in the context file and memory.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from manager.context import gather_all, write_context_prompt
from manager.executor import execute_actions

log = logging.getLogger(__name__)

MANAGER_PROMPT = Path(__file__).parent / "prompt.md"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
DECISIONS_LOG = Path.home() / ".modastack" / "manager" / "decisions.jsonl"


def call_manager(context: dict, prompt_override: str = None) -> tuple[list[dict], str]:
    """Call claude -p with the manager prompt + context.

    Returns (actions, reasoning) — the parsed actions and the manager's
    full response text including any reasoning before the JSON.

    prompt_override: if provided, appended to the manager prompt instead
    of the default context formatting. Used by the event-driven consumer.
    """
    prompt_template = MANAGER_PROMPT.read_text()
    if prompt_override:
        full_prompt = prompt_template + prompt_override
    else:
        context_text = write_context_prompt(context)
        full_prompt = prompt_template + context_text

    result = subprocess.run(
        [CLAUDE, "-p", "--output-format", "json", "--max-turns", "10"],
        input=full_prompt,
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=180,
    )

    # Parse the JSON response — may succeed even with non-zero exit code
    try:
        response = json.loads(result.stdout)
        text = response.get("result", "")
        cost = response.get("total_cost_usd", 0)
        if not text and result.returncode != 0:
            log.error(f"Manager call failed (exit {result.returncode}): {result.stdout[:300]}")
            return [], ""
    except (json.JSONDecodeError, ValueError):
        if result.returncode != 0:
            log.error(f"Manager call failed: {result.stderr[:200]}")
            return [], ""
        text = result.stdout
        cost = 0

    if cost:
        log.info(f"Manager tick cost: ${cost:.4f}")

    # Extract JSON array from the response
    actions = _extract_json_actions(text)
    return actions, text


def _extract_json_actions(text: str) -> list[dict]:
    """Extract a JSON array from the manager's response text."""
    import re

    # Strip markdown code fences
    stripped = re.sub(r'```json\s*', '', text)
    stripped = re.sub(r'```\s*', '', stripped).strip()

    # Try parsing the stripped text as JSON
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except (json.JSONDecodeError, ValueError):
        pass

    # Find the outermost JSON array (greedy match from first [ to last ])
    match = re.search(r'\[[\s\S]*\]', stripped)
    if match:
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            pass

    log.warning(f"Manager returned unparseable response: {text[:200]}")
    return []


async def tick() -> dict:
    """Run one manager tick. Returns summary of actions taken."""
    context = await gather_all()
    actions, reasoning = call_manager(context)

    # Execute actions
    summary = await execute_actions(actions) if actions else {"executed": 0, "skipped": 0, "errors": 0}

    # Log everything: reasoning, decisions, and outcomes
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
        log.info(f"Manager decided {len(actions)} actions: "
                 + ", ".join(a.get("type", "?") for a in actions))

    return summary


def run_loop(interval: int = 60):
    """Run the manager loop forever."""
    log.info(f"Modabot manager starting. Tick every {interval}s.")
    while True:
        try:
            summary = asyncio.run(tick())
            if summary.get("executed", 0) > 0 or summary.get("errors", 0) > 0:
                log.info(f"Tick: {json.dumps(summary)}")
        except Exception as e:
            log.error(f"Tick failed: {e}")
        time.sleep(interval)


def run_once():
    """Run a single manager tick (for testing)."""
    return asyncio.run(tick())


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if "--once" in sys.argv:
        result = run_once()
        print(json.dumps(result, indent=2))
    else:
        interval = 60
        for arg in sys.argv[1:]:
            if arg.isdigit():
                interval = int(arg)
        run_loop(interval)
