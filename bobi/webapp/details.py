"""What to show for a run that has no transcript.

Most rows in the runs table open a transcript. Some cannot: a `$0` script-cache
monitor tick never spawned a session, and neither did a firing that failed
before its agent started. Those rows are not broken — they are the cheap path
working, and the failure path reporting honestly — so they get the other thing
the slab can show: **the run record, plus the definition of the monitor that
produced it.**

That pairing is the point. The record says what happened on this firing; the
definition says what the monitor was asked to do. Debugging a monitor almost
always means holding both, and the definition is otherwise only visible by
reading `agent.yaml` on the box.

Every read takes an explicit root: one webapp process serves every team and
binds none of them.
"""

from __future__ import annotations

from pathlib import Path

# Fields of a monitor definition worth showing. Not `to_dict()` wholesale: a
# definition can carry a `command` with credentials interpolated into it, and
# the slab is a debugging view, not a config dump.
_DEFINITION_FIELDS = ("name", "description", "interval", "at", "tz", "days",
                      "event", "check", "notify", "role", "relevance",
                      "id_field", "enabled")


class UnknownRun(Exception):
    """No run record with that id under this root."""


def _definition(root: Path, monitor_name: str) -> dict:
    """The monitor's resolved definition, or ``{}`` when it no longer exists.

    A record outlives its monitor: a firing recorded last week is still worth
    reading after the monitor was renamed or removed, so a missing definition
    costs that half of the slab, never the response.
    """
    from bobi.monitors.registry import MonitorRegistry

    try:
        monitors = MonitorRegistry.load(project_path=root).all_monitors()
    except Exception:
        return {}
    monitor = next((m for m in monitors if m.name == monitor_name), None)
    if monitor is None:
        return {}
    out = {}
    for field in _DEFINITION_FIELDS:
        value = getattr(monitor, field, None)
        if value not in (None, "", [], {}):
            out[field] = value
    return out


def build_details(root: Path, run_id: str) -> dict:
    """The Details slab for one monitor run.

    Shape::

        {"kind": "monitor",
         "run": {run_id, monitor, started_at, ended_at, outcome, reason,
                 flavor, script_cache_mode, session_ref, published},
         "definition": {...},          # {} when the monitor is gone
         "session_id": str}            # "" when the firing spawned nothing

    Raises ``UnknownRun`` when no record carries that id.
    """
    from bobi.monitors import run_records

    record = next((r for r in run_records.load_all(root) if r.run_id == run_id),
                  None)
    if record is None:
        raise UnknownRun(run_id)
    return {
        "kind": "monitor",
        "run": record.to_json(),
        "definition": _definition(root, record.monitor),
        # Present even here: a firing that DID spawn a session has both a
        # record and a transcript, and the slab should offer the transcript
        # rather than pretend this row has nothing more to show.
        "session_id": record.session_ref,
    }
