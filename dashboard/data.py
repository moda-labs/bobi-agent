"""Read-only data access for the dashboard.

Reads events.jsonl, decisions.jsonl, and tmux session state.
No writes — the dashboard is a view layer over existing state.
"""

import json
import shutil
import subprocess
from pathlib import Path

from modastack.tmux import is_paused

EVENTS_PATH = Path.home() / ".modastack" / "manager" / "events.jsonl"
DECISIONS_PATH = Path.home() / ".modastack" / "manager" / "decisions.jsonl"
TMUX = shutil.which("tmux") or "tmux"


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


def get_sessions() -> list[dict]:
    result = subprocess.run(
        [TMUX, "list-sessions", "-F", "#{session_name} #{session_created}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    sessions = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        name = parts[0]
        if not name.startswith("moda-") or name == "moda-manager":
            continue

        pane = subprocess.run(
            [TMUX, "capture-pane", "-t", name, "-p", "-S", "-10"],
            capture_output=True,
            text=True,
        ).stdout
        pane_lines = [l for l in pane.splitlines() if l.strip()]

        state = "working"
        for l in reversed(pane_lines[-5:]):
            if "❯" in l and "bypass permissions" not in l:
                if any(
                    "bypass permissions" in x or "⏵⏵" in x
                    for x in pane_lines[-3:]
                ):
                    state = "waiting_input"
                break

        last_line = ""
        for l in reversed(pane_lines):
            l = l.strip()
            if l and "─" not in l and "bypass" not in l and "⏵⏵" not in l:
                last_line = l[:100]
                break

        sessions.append({
            "name": name,
            "issue_id": name.replace("moda-", "").upper(),
            "state": state,
            "last_line": last_line,
            "paused": is_paused(name),
        })
    return sessions


def get_manager_status() -> dict:
    result = subprocess.run(
        [TMUX, "has-session", "-t", "moda-manager"],
        capture_output=True,
    )
    alive = result.returncode == 0
    state = "exited"

    if alive:
        pane = subprocess.run(
            [TMUX, "capture-pane", "-t", "moda-manager", "-p", "-S", "-10"],
            capture_output=True,
            text=True,
        ).stdout
        lines = [l for l in pane.splitlines() if l.strip()]
        state = "working"
        for l in reversed(lines[-5:]):
            if "❯" in l and "bypass permissions" not in l:
                if any(
                    "bypass permissions" in x or "⏵⏵" in x
                    for x in lines[-3:]
                ):
                    state = "waiting_input"
                break

    return {"alive": alive, "state": state, "paused": is_paused("moda-manager")}


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
