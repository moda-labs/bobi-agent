"""Read-only data access for the dashboard.

Reads events, activity log, and session registry.
"""

import json
import os
from pathlib import Path

from modastack.config import GLOBAL_CONFIG_DIR
from modastack.sdk import get_registry, ACTIVITY_DIR
from modastack.workflow.state import WorkflowRun
from modastack.workflow.schema import load_workflow

EVENTS_PATH = Path.home() / ".modastack" / "manager" / "events.jsonl"
DECISIONS_PATH = Path.home() / ".modastack" / "manager" / "decisions.jsonl"
ACTIVITY_PATH = ACTIVITY_DIR / "activity.jsonl"
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"


def _read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    result = []
    for line in reversed(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(result) >= limit:
            break
    return result


def read_events(
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
    type_filter: str | None = None,
) -> tuple[list[dict], int]:
    if not EVENTS_PATH.exists():
        return [], 0

    lines = EVENTS_PATH.read_text().strip().splitlines()
    all_events = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if source and event.get("source") != source:
            continue
        if type_filter and type_filter not in event.get("type", ""):
            continue
        all_events.append(event)

    total = len(all_events)
    return all_events[offset : offset + limit], total


def read_decisions(limit: int = 10) -> list[dict]:
    return _read_jsonl_tail(DECISIONS_PATH, limit)


def _is_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def get_manager_status() -> dict:
    from modastack.sdk import load_session_id
    running = _is_running()
    session_id = load_session_id("moda-manager") or ""
    registry = get_registry()
    entry = registry.get("moda-manager")
    status = entry.status if entry else ("running" if running else "stopped")
    return {
        "alive": running,
        "state": status,
        "session_id": session_id[:8] if session_id else "",
    }


def get_sessions() -> list[dict]:
    registry = get_registry()
    engineers = registry.get_by_role("engineer")
    result = []
    for e in engineers:
        result.append({
            "name": e.name,
            "issue_id": e.issue_id,
            "title": e.title,
            "phase": e.phase,
            "repo": e.repo,
            "cwd": e.cwd,
            "status": e.status,
            "started_at": e.started_at,
        })
    return result


def get_conversation_log(limit: int = 50) -> list[dict]:
    if not ACTIVITY_PATH.exists():
        return []
    lines = ACTIVITY_PATH.read_text().strip().splitlines()
    turns = []
    for line in reversed(lines):
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(turns) >= limit:
            break
    return turns


def get_event_sources() -> list[str]:
    if not EVENTS_PATH.exists():
        return []
    sources = set()
    for line in EVENTS_PATH.read_text().strip().splitlines()[-200:]:
        try:
            sources.add(json.loads(line).get("source", ""))
        except json.JSONDecodeError:
            continue
    return sorted(s for s in sources if s)


def _load_workflow_labels(workflow_name: str) -> dict[str, str]:
    from modastack.workflow.triggers import WORKFLOWS_DIR, USER_WORKFLOWS_DIR
    for d in [USER_WORKFLOWS_DIR, WORKFLOWS_DIR]:
        path = d / f"{workflow_name}.yaml"
        if path.exists():
            try:
                wf = load_workflow(path)
                return {nid: n.label for nid, n in wf.nodes.items() if n.label}
            except Exception:
                pass
    return {}


def get_workflow_progress(issue_id: str) -> dict | None:
    for run in WorkflowRun.list_runs():
        trigger_data = run.trigger_event.get("data", {})
        rid = trigger_data.get("issue_id", "")
        if rid.lstrip("#").lower() == issue_id.lower():
            labels = _load_workflow_labels(run.workflow_name)
            nodes = []
            for nid, ns in run.nodes.items():
                nodes.append({
                    "id": nid,
                    "label": labels.get(nid, ""),
                    "status": ns.status,
                })
            return {
                "run_id": run.run_id,
                "workflow": run.workflow_name,
                "status": run.status,
                "nodes": nodes,
            }
    return None
