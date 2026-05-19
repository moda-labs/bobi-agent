"""Gather all context for the brain's tick.

Reads events from Linear, Slack, GitHub, and worker tmux sessions.
Writes a single context file that the brain reads to make decisions.
"""

import json
import logging
import time
from pathlib import Path

from dispatch.config import GlobalConfig, RepoConfig
from dispatch.conversation import get_latest_human_reply_after_agent
from dispatch.linear_api import get_state_ids
from dispatch.scanner import scan_linear_all_active
from dispatch.session import detect_state, capture, session_exists, list_sessions
from dispatch.state import StateStore
from dispatch.summarizer import detect_phase

log = logging.getLogger(__name__)

BRAIN_DIR = Path.home() / ".dispatch" / "brain"
CONTEXT_PATH = BRAIN_DIR / "context.json"
MEMORY_PATH = BRAIN_DIR / "memory.md"


async def gather_linear(api_key: str, repo_config: RepoConfig) -> list[dict]:
    """Gather all active issues from Linear."""
    issues_by_state = await scan_linear_all_active(api_key, repo_config)
    result = []
    for state_name, issues in issues_by_state.items():
        for issue in issues:
            labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
            result.append({
                "id": issue["identifier"],
                "linear_id": issue["id"],
                "title": issue["title"],
                "description": (issue.get("description") or "")[:500],
                "state": state_name,
                "labels": labels,
                "repo": str(repo_config.path),
                "project": repo_config.linear_project,
            })
    return result


def gather_workers() -> list[dict]:
    """Gather status of all active tmux worker sessions."""
    store = StateStore()
    workers = []
    for agent in store.all_agents():
        iid = agent.issue_id
        alive = session_exists(iid)
        sess_state = detect_state(iid) if alive else {"state": "exited"}

        worker = {
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

        # Get last few lines of output for context
        if alive:
            pane = capture(iid, lines=10)
            content_lines = [l.strip() for l in pane.splitlines()
                             if l.strip() and "─" not in l and "bypass" not in l
                             and not l.strip().startswith("▐") and not l.strip().startswith("▝")]
            worker["recent_output"] = "\n".join(content_lines[-5:])

        workers.append(worker)
    return workers


def gather_worktree_phases(repos: list[Path]) -> dict:
    """Check worktree state for all known worktrees."""
    phases = {}
    for repo_path in repos:
        wt_dir = repo_path / "worktrees"
        if not wt_dir.exists():
            continue
        for child in wt_dir.iterdir():
            if child.is_dir():
                issue_id = child.name.upper()
                try:
                    phase_info = detect_phase(str(child))
                    phases[issue_id] = phase_info
                except Exception:
                    pass
    return phases


async def gather_all() -> dict:
    """Gather full context for the brain. Returns the context dict."""
    global_config = GlobalConfig.load()
    all_issues = []
    repos = []

    for repo_path in global_config.repos:
        if not repo_path.exists():
            continue
        try:
            repo_config = RepoConfig.from_file(repo_path)
        except FileNotFoundError:
            continue

        creds = repo_config.get_credentials()
        api_key = creds.get("linear_api_key") or global_config.linear_api_key
        if not api_key:
            continue

        issues = await gather_linear(api_key, repo_config)
        all_issues.extend(issues)
        repos.append(repo_path)

    workers = gather_workers()
    worktree_phases = gather_worktree_phases(repos)

    # Load persistent memory
    memory = ""
    if MEMORY_PATH.exists():
        memory = MEMORY_PATH.read_text()

    context = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repos": [str(r) for r in repos],
        "issues": all_issues,
        "workers": workers,
        "worktree_phases": worktree_phases,
        "memory": memory,
    }

    # Write context file
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(json.dumps(context, indent=2, default=str))
    log.info(f"Context gathered: {len(all_issues)} issues, {len(workers)} workers")
    return context


def write_context_prompt(context: dict) -> str:
    """Format the context dict into a prompt the brain can read."""

    lines = [f"# Modabot Brain Tick — {context['timestamp']}", ""]

    # Repos
    lines.append("## Repos")
    for r in context["repos"]:
        lines.append(f"- {r}")
    lines.append("")

    # Issues by state
    lines.append("## Linear Issues")
    by_state = {}
    for issue in context["issues"]:
        by_state.setdefault(issue["state"], []).append(issue)

    for state in ["Todo", "In Progress", "Blocked", "In Review", "Done"]:
        issues = by_state.get(state, [])
        if not issues:
            continue
        lines.append(f"\n### {state}")
        for i in issues:
            labels = ", ".join(i["labels"]) if i["labels"] else "no labels"
            lines.append(f"- **{i['id']}** (linear_id: {i['linear_id']}): {i['title']} [{i['project']}] ({labels}) repo: {i['repo']}")
            if i["description"]:
                lines.append(f"  {i['description'][:200]}")

    # Workers
    lines.append("\n## Active Workers")
    if not context["workers"]:
        lines.append("No active workers.")
    for w in context["workers"]:
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

    # Memory
    if context["memory"]:
        lines.append(f"\n## Memory\n{context['memory']}")

    return "\n".join(lines)
