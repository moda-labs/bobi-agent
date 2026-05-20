"""Engineer tmux session monitoring channel."""

import time

from dispatch.session import detect_state, capture, session_exists
from dispatch.state import StateStore


async def gather(config: dict) -> list[dict]:
    """Check status of all active engineer tmux sessions."""
    store = StateStore()
    items = []

    for agent in store.all_agents():
        iid = agent.issue_id
        alive = session_exists(iid)
        sess_state = detect_state(iid) if alive else {"state": "exited"}

        worker = {
            "channel": "worker",
            "issue_id": iid,
            "title": agent.title,
            "phase": agent.last_phase,
            "session_state": sess_state["state"],
            "alive": alive,
            "started_minutes_ago": int((time.time() - agent.started_at) / 60),
            "idle_minutes": int((time.time() - agent.last_activity_at) / 60),
            "tmux_session": f"agentd-{iid.lower()}",
        }

        if sess_state.get("question"):
            worker["question"] = sess_state["question"]
            worker["options"] = sess_state.get("options", [])

        if alive:
            pane = capture(iid, lines=10)
            content_lines = [l.strip() for l in pane.splitlines()
                             if l.strip() and "─" not in l and "bypass" not in l
                             and not l.strip().startswith("▐") and not l.strip().startswith("▝")]
            worker["recent_output"] = "\n".join(content_lines[-5:])

        items.append(worker)
    return items


def hash_key(items: list[dict]) -> str:
    """Change detection — session state + phase."""
    parts = []
    for w in items:
        parts.append(f"{w['issue_id']}:{w['session_state']}:{w['phase']}")
    return "|".join(sorted(parts))


def format_context(items: list[dict]) -> str:
    """Format for manager prompt."""
    lines = ["\n## Active Engineers"]
    if not items:
        lines.append("No active engineers.")
        return "\n".join(lines)

    for w in items:
        lines.append(f"\n### {w['issue_id']}: {w['title']}")
        lines.append(f"- Session: {w['tmux_session']} ({w['session_state']})")
        lines.append(f"- Phase: {w['phase']}")
        lines.append(f"- Running: {w['started_minutes_ago']}m, idle: {w['idle_minutes']}m")
        if w.get("question"):
            lines.append(f"- **QUESTION**: {w['question']}")
            for opt in w.get("options", []):
                lines.append(f"  - {opt}")
        if w.get("recent_output"):
            lines.append(f"- Recent output:\n```\n{w['recent_output']}\n```")
    return "\n".join(lines)
