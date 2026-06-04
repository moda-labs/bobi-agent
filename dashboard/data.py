"""Read-only data access for the dashboard.

Reads events, activity log, and session registry.
"""

import json
import os
from pathlib import Path

from modastack.config import GLOBAL_CONFIG_DIR
from modastack.sdk import get_registry, SessionRegistry
from modastack.workflow.state import WorkflowRun
from modastack.workflow.schema import load_workflow

EVENTS_PATH = Path.home() / ".modastack" / "manager" / "events.jsonl"
DECISIONS_PATH = Path.home() / ".modastack" / "manager" / "decisions.jsonl"
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"
MODASTACK_LOG_PATH = GLOBAL_CONFIG_DIR / "modastack.log"


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Return the last `limit` lines of a file without reading it whole.

    Seeks backward from the end in chunks so an unbounded log (the
    modastack daemon's stdout sink never rotates) stays cheap to poll.
    """
    if not path.exists():
        return []
    block = 64 * 1024
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        newlines = 0
        pos = size
        while pos > 0 and newlines <= limit:
            read = min(block, pos)
            pos -= read
            f.seek(pos)
            chunk = f.read(read)
            data = chunk + data
            newlines += chunk.count(b"\n")
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


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


def _activity_snippet(session: str, length: int = 120) -> str:
    """Most recent assistant response text for a session, truncated.

    Only scans the tail of the log — the latest response is near the end,
    and these files are polled on the async /api/status path.
    """
    log_path = SessionRegistry.log_path(session)
    for line in reversed(_tail_lines(log_path, 200)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "response":
            text = (entry.get("text") or "").strip().replace("\n", " ")
            return text[:length] + ("…" if len(text) > length else "")
    return ""


def _get_manager_session_name() -> str:
    from modastack.manager.session import get_default_session
    session = get_default_session()
    return session.session_name if session else "moda-manager"


def get_manager_status() -> dict:
    from modastack.sdk import load_session_id
    name = _get_manager_session_name()
    running = _is_running()
    session_id = load_session_id(name) or ""
    registry = get_registry()
    entry = registry.get(name)
    status = entry.status if entry else ("running" if running else "stopped")
    return {
        "alive": running,
        "state": status,
        "session_id": session_id[:8] if session_id else "",
        "activity": _activity_snippet(name),
        "last_activity": entry.last_activity if entry else 0,
    }


def get_sessions() -> list[dict]:
    registry = get_registry()
    engineers = [e for e in registry.get_by_role("engineer") if e.status not in ("done", "cancelled")]
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
            "last_activity": e.last_activity,
            "activity": _activity_snippet(e.name),
        })
    return result


def read_modastack_log(limit: int = 200) -> list[str]:
    """Return the last `limit` lines of the modastack process log."""
    return _tail_lines(MODASTACK_LOG_PATH, limit)


def get_conversation_log(limit: int = 50, session: str = "") -> list[dict]:
    if not session:
        session = _get_manager_session_name()
    log_path = SessionRegistry.log_path(session)
    if not log_path.exists():
        return []
    lines = log_path.read_text().strip().splitlines()
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


def _load_workflow_info(workflow_name: str) -> tuple[dict[str, str], list[str]]:
    from modastack.workflow.triggers import WORKFLOWS_DIR, USER_WORKFLOWS_DIR
    for d in [USER_WORKFLOWS_DIR, WORKFLOWS_DIR]:
        path = d / f"{workflow_name}.yaml"
        if path.exists():
            try:
                wf = load_workflow(path)
                labels = {nid: n.label for nid, n in wf.nodes.items() if n.label}
                order = wf.topological_order()
                return labels, order
            except Exception:
                pass
    return {}, []


def get_workflow_progress(issue_id: str) -> dict | None:
    for run in WorkflowRun.list_runs():
        trigger_data = run.trigger_event.get("data", {})
        rid = trigger_data.get("issue_id", "")
        if rid.lstrip("#").lower() == issue_id.lower():
            labels, all_node_ids = _load_workflow_info(run.workflow_name)
            nodes = []
            for nid in all_node_ids:
                ns = run.nodes.get(nid)
                nodes.append({
                    "id": nid,
                    "label": labels.get(nid, ""),
                    "status": ns.status if ns else "pending",
                })
            return {
                "run_id": run.run_id,
                "workflow": run.workflow_name,
                "status": run.status,
                "nodes": nodes,
            }
    return None
