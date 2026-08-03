"""What the script-cache runner did not spend.

A description-only monitor pays for an LLM call every interval to re-reason the
same check. A `script_cache` monitor pays once — the agent works the check out
and emits a shell script — and every later tick runs that script for nothing.
The runner records both halves per monitor in
``run/state/scripts/<name>.state.json``: how many ticks ran cached, how many
fell back to an agent, and what those agent ticks actually cost.

That is enough to say what the cache saved, and the honest way to say it is
each monitor's own arithmetic: **this monitor's cached ticks × what this
monitor's agent ticks actually cost on average.** A fleet-wide average would
price a cheap monitor's savings with an expensive one's bill.

Two things this deliberately does not do:

- **No modelled price for a monitor that never paid one.** With no agent tick
  on record there is no basis, so the saving is not estimated at all — the
  cached-run count still tells the true story, and a number with nothing under
  it would not.
- **No summing with recorded spend.** Savings are counterfactual dollars. They
  belong beside a bill, never inside one.
"""

from __future__ import annotations

import json
from pathlib import Path


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _int(value) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _float(value) -> float:
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def script_cache_savings(root: Path) -> dict:
    """Aggregate the script-cache ledgers under one runtime root.

    Shape::

        {"monitors":        int,    # monitors with a cache ledger
         "cached_runs":     int,    # ticks that ran the script, ~$0
         "agent_runs":      int,    # ticks that paid an agent
         "agent_cost_usd":  float,  # what those paid ticks cost, recorded
         "estimated_savings_usd": float,   # per-monitor arithmetic, summed
         "priced_monitors": int}    # how many had a basis to price with

    ``priced_monitors`` is the honesty dial: when it is 0, the saving is 0
    because nothing could be priced, not because nothing was saved. A caller
    showing dollars should show the cached-run count beside them.
    """
    from bobi import paths

    # state_path, not state_dir: a read must not create directories in
    # someone else's runtime tree.
    scripts_dir = paths.state_path(root) / "scripts"
    totals = {"monitors": 0, "cached_runs": 0, "agent_runs": 0,
              "agent_cost_usd": 0.0, "estimated_savings_usd": 0.0,
              "priced_monitors": 0}
    if not scripts_dir.exists():
        return totals

    for path in sorted(scripts_dir.glob("*.state.json")):
        state = _read(path)
        if not state:
            continue
        cached = _int(state.get("cached_runs"))
        agent_runs = _int(state.get("fallback_runs"))
        agent_cost = _float(state.get("total_agent_cost_usd"))
        totals["monitors"] += 1
        totals["cached_runs"] += cached
        totals["agent_runs"] += agent_runs
        totals["agent_cost_usd"] += agent_cost
        if cached and agent_runs and agent_cost:
            totals["priced_monitors"] += 1
            totals["estimated_savings_usd"] += cached * (agent_cost / agent_runs)

    totals["agent_cost_usd"] = round(totals["agent_cost_usd"], 6)
    totals["estimated_savings_usd"] = round(totals["estimated_savings_usd"], 6)
    return totals
