"""Daemon: poll Linear, manage tmux sessions, bridge questions to humans.

Each issue gets one persistent interactive Claude Code session in tmux.
The daemon monitors sessions, detects questions, injects replies, and
routes phases — all within the same session so context is preserved.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import get_latest_human_reply_after_agent
from .linear_api import get_state_ids, move_issue, add_comment
from .scanner import scan_linear_all_active
from .session import (
    session_exists, spawn_session, inject, capture,
    detect_state, answer_question, kill_session,
)
from .state import StateStore

log = logging.getLogger(__name__)

STALL_TIMEOUT = 600  # 10 minutes

PHASE_ROUTES = {
    "triage_complete":          lambda h: "spec" if h.get("needs_spec") == "true" else "implement",
    "spec_complete":            lambda h: None,
    "blocked":                  lambda h: None,
    "implementation_complete":  lambda h: "ship-pr",
    "feedback_addressed":       lambda h: "ship-pr",
    "in_review":                lambda h: None,
}

PHASE_LINEAR_STATE = {
    "triage_complete":          "In Progress",
    "spec_complete":            "In Progress",
    "blocked":                  "Blocked",
    "implementation_complete":  "In Progress",
    "feedback_addressed":       "In Review",
    "in_review":                "In Review",
}


def _read_handoff(worktree: str) -> dict | None:
    hf = Path(worktree) / ".dispatch" / "handoff.md"
    if not hf.exists():
        return None
    text = hf.read_text()
    match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not match:
        return None
    import yaml
    try:
        return yaml.safe_load(match.group(1))
    except Exception:
        return None


def _find_worktree(repo_path: Path, issue_id: str) -> str | None:
    wt_dir = repo_path / "worktrees"
    if not wt_dir.exists():
        return None
    prefix = issue_id.lower()
    for child in wt_dir.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            return str(child)
    return None


def _patch_handoff_phase(worktree: str, new_phase: str) -> None:
    hf = Path(worktree) / ".dispatch" / "handoff.md"
    if not hf.exists():
        return
    text = hf.read_text()
    patched = re.sub(r"^phase: .+$", f"phase: {new_phase}", text, count=1, flags=re.MULTILINE)
    hf.write_text(patched)


def _infer_phase(worktree: str, handoff_phase: str) -> str:
    gh = shutil.which("gh") or "gh"
    pr_result = subprocess.run(
        [gh, "pr", "view", "--json", "url,state"],
        cwd=worktree, capture_output=True, text=True,
    )
    pr_url = None
    if pr_result.returncode == 0:
        import json
        try:
            data = json.loads(pr_result.stdout)
            if data.get("state") in ("OPEN", "MERGED"):
                pr_url = data.get("url")
        except (json.JSONDecodeError, ValueError):
            pass

    if pr_url and handoff_phase in ("triage_complete", "implementation_complete"):
        log.info(f"Inferred: in_review (PR exists, handoff says {handoff_phase})")
        return "in_review"

    if handoff_phase == "triage_complete":
        result = subprocess.run(
            ["git", "diff", "--name-only", "main..HEAD"],
            cwd=worktree, capture_output=True, text=True,
        )
        changed = result.stdout.strip().splitlines() if result.returncode == 0 else []
        non_spec = [f for f in changed if not f.startswith("specs/") and not f.startswith(".dispatch")]
        if non_spec:
            log.info(f"Inferred: implementation_complete (code committed, handoff says {handoff_phase})")
            return "implementation_complete"

        specs_dir = Path(worktree) / "specs"
        if specs_dir.exists() and any(f.suffix == ".md" for f in specs_dir.iterdir()):
            log.info(f"Inferred: spec_complete (spec exists, handoff says {handoff_phase})")
            return "spec_complete"

    return handoff_phase


async def run_cycle() -> dict:
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"dispatched": 0, "killed": 0, "done": 0, "continued": 0, "questions": 0}

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

        env = os.environ.copy()
        env["LINEAR_API_KEY"] = api_key

        issues_by_state = await scan_linear_all_active(api_key, repo_config)
        state_ids = await get_state_ids(api_key, repo_config.linear_project)

        linear_lookup = {}
        for state_name, issues in issues_by_state.items():
            for issue in issues:
                linear_lookup[issue["identifier"]] = (state_name, issue)

        # --- Monitor active sessions ---
        for agent in list(state.agents_for_repo(str(repo_path))):
            iid = agent.issue_id
            linear_info = linear_lookup.get(iid)
            linear_id = linear_info[1]["id"] if linear_info else agent.linear_issue_id

            # Terminal state — kill session
            if linear_info and linear_info[0] in ("Done", "Canceled", "Cancelled"):
                if session_exists(iid):
                    kill_session(iid)
                    summary["killed"] += 1
                state.remove(iid)
                continue

            sess_state = detect_state(iid)

            if sess_state["state"] == "exited":
                # Session died — handle handoff
                state.remove(iid)
                wt = _find_worktree(repo_path, iid)
                if not wt:
                    continue
                handoff = _read_handoff(wt)
                if not handoff:
                    log.info(f"{iid}: session exited with no handoff")
                    continue

                phase = handoff.get("phase", "")
                inferred = _infer_phase(wt, phase)
                if inferred != phase:
                    _patch_handoff_phase(wt, inferred)
                    phase = inferred

                # Update Linear
                target = PHASE_LINEAR_STATE.get(phase)
                if target and target in state_ids and linear_id:
                    current = linear_info[0] if linear_info else ""
                    if current != target:
                        await move_issue(api_key, linear_id, state_ids[target])

                if phase == "spec_complete" and linear_id:
                    pr_url = handoff.get("pr_url", "")
                    msg = "Spec ready for review."
                    if pr_url:
                        msg += f" PR: {pr_url}"
                    msg += "\n\nReply **approved** to start implementation."
                    await add_comment(api_key, linear_id, msg)

                if phase == "blocked" and linear_id:
                    question = handoff.get("question", "Agent is blocked.")
                    await add_comment(api_key, linear_id, f"**Question:**\n\n{question}")

                if phase == "in_review" and linear_id:
                    pr_url = handoff.get("pr_url", "")
                    await add_comment(api_key, linear_id, f"Ready for review. PR: {pr_url}")

                log.info(f"{iid}: session exited, phase={phase}")
                continue

            if sess_state["state"] == "asking_question":
                # Agent is asking a question — post to Linear
                question = sess_state.get("question", "Agent has a question")
                options = sess_state.get("options", [])
                options_text = "\n".join(f"- {o}" for o in options)
                msg = f"**Agent question:**\n\n{question}\n\n{options_text}\n\nReply with your choice."

                if linear_id and agent.last_phase != "_question_posted":
                    await add_comment(api_key, linear_id, msg)
                    state.set_phase(iid, "_question_posted")
                    if "Blocked" in state_ids:
                        current = linear_info[0] if linear_info else ""
                        if current != "Blocked":
                            await move_issue(api_key, linear_id, state_ids["Blocked"])
                    summary["questions"] += 1
                    log.info(f"{iid}: posted question to Linear")
                continue

            if sess_state["state"] == "waiting_input":
                # Agent is idle — check if handoff was updated
                wt = _find_worktree(repo_path, iid)
                if not wt:
                    continue

                handoff = _read_handoff(wt)
                if not handoff:
                    continue

                phase = handoff.get("phase", "")
                if phase == agent.last_phase:
                    # No phase change — check for stall
                    elapsed = time.time() - agent.last_activity_at
                    if elapsed > STALL_TIMEOUT:
                        kill_session(iid)
                        state.remove(iid)
                        summary["killed"] += 1
                        log.warning(f"{iid}: stalled ({int(elapsed)}s)")
                    continue

                # Phase changed — update tracking and route
                state.touch(iid)
                state.set_phase(iid, phase)

                # Update Linear
                target = PHASE_LINEAR_STATE.get(phase)
                if target and target in state_ids and linear_id:
                    current = linear_info[0] if linear_info else ""
                    if current != target:
                        await move_issue(api_key, linear_id, state_ids[target])

                if phase == "spec_complete" and linear_id:
                    pr_url = handoff.get("pr_url", "")
                    msg = "Spec ready for review."
                    if pr_url:
                        msg += f" PR: {pr_url}"
                    msg += "\n\nReply **approved** to start implementation."
                    await add_comment(api_key, linear_id, msg)

                if phase == "in_review" and linear_id:
                    pr_url = handoff.get("pr_url", "")
                    await add_comment(api_key, linear_id, f"Ready for review. PR: {pr_url}")

                # Route to next skill — inject into SAME session
                router = PHASE_ROUTES.get(phase)
                if router:
                    next_skill = router(handoff)
                    if next_skill:
                        inject(iid, f"/invoke /{next_skill} for issue {iid}")
                        summary["continued"] += 1
                        log.info(f"{iid}: phase '{phase}' → /{next_skill} (same session)")

                continue

            # Working — update activity
            if sess_state["state"] == "working":
                state.touch(iid)

        # --- Check for merged PRs ---
        for issue in issues_by_state.get("In Review", []):
            iid = issue["identifier"]
            if state.is_tracked(iid):
                continue
            wt = _find_worktree(repo_path, iid)
            if not wt:
                continue
            result = subprocess.run(
                [shutil.which("gh") or "gh", "pr", "view", "--json", "state"],
                cwd=wt, capture_output=True, text=True,
            )
            if result.returncode == 0 and '"MERGED"' in result.stdout:
                if "Done" in state_ids:
                    await move_issue(api_key, issue["id"], state_ids["Done"])
                    await add_comment(api_key, issue["id"], "PR merged. Issue complete.")
                if session_exists(iid):
                    kill_session(iid)
                log.info(f"{iid} → Done (PR merged)")
                summary["done"] += 1

        # --- Dispatch new Todo issues ---
        for issue in issues_by_state.get("Todo", []):
            iid = issue["identifier"]
            labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
            if not any(t in labels for t in repo_config.trigger_labels):
                continue
            if state.is_tracked(iid):
                continue
            if len(state.agents_for_repo(str(repo_path))) >= repo_config.max_parallel:
                break

            # Move to In Progress
            if "In Progress" in state_ids:
                await move_issue(api_key, issue["id"], state_ids["In Progress"])
            await add_comment(api_key, issue["id"], "Picked up by agentd.")

            # Spawn tmux session
            ok = spawn_session(iid, cwd=str(repo_path))
            if not ok:
                continue

            # Inject the pickup task
            context = f"Title: {issue['title']}\n\n{issue.get('description', '')}"
            inject(iid, f"/pickup {iid}\n\n{context}")

            state.track(issue_id=iid, repo_path=str(repo_path),
                        title=issue["title"], worktree=str(repo_path),
                        linear_issue_id=issue["id"])
            summary["dispatched"] += 1
            log.info(f"Dispatched {iid} (tmux session agentd-{iid.lower()})")

        # --- Inject human replies into active sessions ---
        for linear_state in ["Blocked", "In Review", "In Progress"]:
            for issue in issues_by_state.get(linear_state, []):
                iid = issue["identifier"]
                if not state.is_tracked(iid):
                    continue
                if not session_exists(iid):
                    continue

                labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                if not any(t in labels for t in repo_config.trigger_labels):
                    continue

                reply = await get_latest_human_reply_after_agent(api_key, issue["id"])
                if not reply:
                    continue

                agent = state.get(iid)
                if not agent or agent.last_phase != "_question_posted":
                    continue

                sess_state = detect_state(iid)
                if sess_state["state"] == "asking_question":
                    # Try to match reply to an option
                    options = sess_state.get("options", [])
                    matched = False
                    for i, opt in enumerate(options, 1):
                        if reply.strip().lower() in opt.lower():
                            answer_question(iid, choice=i)
                            matched = True
                            break
                    if not matched:
                        answer_question(iid, text=reply)
                elif sess_state["state"] == "waiting_input":
                    # Session is at prompt — inject the reply directly
                    inject(iid, reply)

                state.set_phase(iid, "")  # clear question_posted flag
                state.touch(iid)

                if "In Progress" in state_ids:
                    await move_issue(api_key, issue["id"], state_ids["In Progress"])

                log.info(f"{iid}: injected human reply into session")

    return summary


def run() -> dict:
    return asyncio.run(run_cycle())
