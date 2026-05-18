"""Thin daemon: poll Linear, route to skills, track PIDs.

Each skill is an atomic phase. When an agent exits, the daemon reads
.dispatch/handoff.md to determine the next skill to spawn. Skills never
chain themselves — the daemon is the state machine.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import GlobalConfig, RepoConfig
from .conversation import get_latest_human_reply_after_agent
from .linear_api import get_state_ids, move_issue, add_comment
from .scanner import scan_linear_all_active
from .state import StateStore

log = logging.getLogger(__name__)

STALL_TIMEOUT = 600  # 10 minutes

# Handoff phase → next skill to spawn
PHASE_ROUTES = {
    "triage_complete":          lambda h: "spec" if h.get("needs_spec") == "true" else "implement",
    "spec_complete":            lambda h: None,  # wait for human approval
    "blocked":                  lambda h: None,  # wait for human reply
    "implementation_complete":  lambda h: "ship-pr",
    "feedback_addressed":       lambda h: "ship-pr",
    "in_review":                lambda h: None,  # wait for human
}

# Handoff phase → Linear state the issue should be in
PHASE_LINEAR_STATE = {
    "triage_complete":          "In Progress",
    "spec_complete":            "In Progress",  # still in progress, waiting for review
    "blocked":                  "Blocked",
    "implementation_complete":  "In Progress",
    "feedback_addressed":       "In Review",
    "in_review":                "In Review",
}


def _read_handoff(worktree: str) -> dict | None:
    """Read .dispatch/handoff.md YAML frontmatter. Returns dict or None."""
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


def _spawn_skill(skill: str, issue_id: str, worktree: str | None,
                 repo_path: Path, env: dict, context: str = "") -> int:
    """Spawn claude with a skill invocation. Returns PID."""
    claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
    cwd = worktree or str(repo_path)

    prompt = f"Invoke /{skill} for issue {issue_id}."
    if context:
        prompt += f"\n\n{context}"

    proc = subprocess.Popen(
        [claude, "-p", prompt, "--max-turns", "200", "--dangerously-skip-permissions"],
        cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    log.info(f"Spawned /{skill} for {issue_id} (PID {proc.pid}) in {cwd}")
    return proc.pid


def _find_worktree(repo_path: Path, issue_id: str) -> str | None:
    wt_dir = repo_path / "worktrees"
    if not wt_dir.exists():
        return None
    prefix = issue_id.lower()
    for child in wt_dir.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            return str(child)
    return None


def _has_commits(worktree: str) -> bool:
    """Check if the worktree branch has commits beyond main."""
    result = subprocess.run(
        ["git", "log", "--oneline", "main..HEAD"],
        cwd=worktree, capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _has_open_pr(worktree: str) -> str | None:
    """Check if the branch has an open PR. Returns URL or None."""
    gh = shutil.which("gh") or "gh"
    result = subprocess.run(
        [gh, "pr", "view", "--json", "url,state"],
        cwd=worktree, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    import json
    try:
        data = json.loads(result.stdout)
        if data.get("state") in ("OPEN", "MERGED"):
            return data.get("url")
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _has_spec(worktree: str) -> bool:
    """Check if a spec file exists."""
    specs = Path(worktree) / "specs"
    if specs.exists():
        return any(f.suffix == ".md" for f in specs.iterdir())
    return False


def _patch_handoff_phase(worktree: str, new_phase: str) -> None:
    """Update just the phase field in the handoff file."""
    hf = Path(worktree) / ".dispatch" / "handoff.md"
    if not hf.exists():
        return
    text = hf.read_text()
    patched = re.sub(r"^phase: .+$", f"phase: {new_phase}", text, count=1, flags=re.MULTILINE)
    hf.write_text(patched)


def _infer_phase(worktree: str, handoff_phase: str) -> str:
    """Infer the actual phase from worktree state when the handoff is stale.

    The agent may have done work but failed to update the handoff.
    Check the worktree for evidence of progress and return the real phase.
    """
    pr_url = _has_open_pr(worktree)
    has_code = _has_commits(worktree)
    has_spec_file = _has_spec(worktree)

    # If there's already an open PR, we're past implementation
    if pr_url and handoff_phase in ("triage_complete", "implementation_complete"):
        log.info(f"Inferred phase: in_review (PR exists but handoff says {handoff_phase})")
        return "in_review"

    # If there are commits with non-spec code and handoff still says triage
    if has_code and handoff_phase == "triage_complete":
        # Check if commits are spec-only or include implementation
        result = subprocess.run(
            ["git", "diff", "--name-only", "main..HEAD"],
            cwd=worktree, capture_output=True, text=True,
        )
        changed = result.stdout.strip().splitlines() if result.returncode == 0 else []
        non_spec = [f for f in changed if not f.startswith("specs/") and not f.startswith(".dispatch")]
        if non_spec:
            log.info(f"Inferred phase: implementation_complete (code committed but handoff says {handoff_phase})")
            return "implementation_complete"
        elif has_spec_file:
            log.info(f"Inferred phase: spec_complete (spec committed but handoff says {handoff_phase})")
            return "spec_complete"

    return handoff_phase


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def run_cycle() -> dict:
    global_config = GlobalConfig.load()
    state = StateStore()
    summary = {"dispatched": 0, "killed": 0, "done": 0, "continued": 0}

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

        # Build lookup: issue identifier → Linear data
        linear_lookup = {}
        for state_name, issues in issues_by_state.items():
            for issue in issues:
                linear_lookup[issue["identifier"]] = (state_name, issue)

        # --- Phase 1: Handle exited agents (read handoff, route next skill) ---
        for agent in list(state.agents_for_repo(str(repo_path))):
            if _is_alive(agent.pid):
                # Update activity timestamp from worktree changes
                wt = Path(agent.worktree)
                for f in [wt / ".dispatch" / "handoff.md", wt / ".dispatch" / "state.md"]:
                    if f.exists() and f.stat().st_mtime > agent.last_activity_at:
                        state.touch(agent.issue_id)
                        break

                # Kill if stalled
                elapsed = time.time() - agent.last_activity_at
                if elapsed > STALL_TIMEOUT:
                    try:
                        os.kill(agent.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    state.remove(agent.issue_id)
                    summary["killed"] += 1
                    log.warning(f"Killed stalled agent {agent.issue_id} ({int(elapsed)}s)")
                continue

            # Agent exited — read handoff to decide next step
            state.remove(agent.issue_id)
            wt = _find_worktree(repo_path, agent.issue_id)
            if not wt:
                continue

            handoff = _read_handoff(wt)
            if not handoff:
                log.info(f"{agent.issue_id}: exited with no handoff")
                continue

            phase = handoff.get("phase", "")

            # Resilience: if the agent did work but didn't update the handoff,
            # infer the correct phase from worktree state
            inferred = _infer_phase(wt, phase)
            if inferred != phase:
                # Write the corrected phase back so we don't re-infer next cycle
                _patch_handoff_phase(wt, inferred)
                phase = inferred
            linear_info = linear_lookup.get(agent.issue_id)
            linear_id = linear_info[1]["id"] if linear_info else agent.linear_issue_id

            # Move Linear state if needed
            target_state = PHASE_LINEAR_STATE.get(phase)
            if target_state and target_state in state_ids and linear_id:
                current = linear_info[0] if linear_info else ""
                if current != target_state:
                    await move_issue(api_key, linear_id, state_ids[target_state])
                    log.info(f"{agent.issue_id}: {current} → {target_state}")

            # Post to Linear on key transitions
            if phase == "spec_complete" and linear_id:
                pr_url = handoff.get("pr_url", "")
                msg = f"Spec ready for review."
                if pr_url:
                    msg += f" PR: {pr_url}"
                msg += "\n\nReply **approved** to start implementation."
                await add_comment(api_key, linear_id, msg)

            if phase == "blocked" and linear_id:
                question = handoff.get("question", "Agent is blocked and needs input.")
                await add_comment(api_key, linear_id, f"**Question:**\n\n{question}")

            if phase == "in_review" and linear_id:
                pr_url = handoff.get("pr_url", "")
                await add_comment(api_key, linear_id, f"Ready for review. PR: {pr_url}")

            # Route to next skill
            router = PHASE_ROUTES.get(phase)
            if not router:
                log.info(f"{agent.issue_id}: phase '{phase}' — waiting")
                continue

            next_skill = router(handoff)
            if not next_skill:
                log.info(f"{agent.issue_id}: phase '{phase}' — waiting for human")
                continue

            pid = _spawn_skill(next_skill, agent.issue_id, wt, repo_path, env)
            state.track(issue_id=agent.issue_id, pid=pid, repo_path=str(repo_path),
                        title=agent.title, worktree=wt,
                        linear_issue_id=linear_id)
            summary["continued"] += 1
            log.info(f"{agent.issue_id}: phase '{phase}' → /{next_skill}")

        # --- Phase 2: Check for merged PRs (no agent needed) ---
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
                log.info(f"{iid} → Done (PR merged)")
                summary["done"] += 1

        # --- Phase 3: Dispatch new Todo issues → /pickup ---
        for issue in issues_by_state.get("Todo", []):
            iid = issue["identifier"]
            labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
            if not any(t in labels for t in repo_config.trigger_labels):
                continue
            if state.is_tracked(iid):
                continue
            if len(state.agents_for_repo(str(repo_path))) >= repo_config.max_parallel:
                break

            # Move to In Progress before spawning
            if "In Progress" in state_ids:
                await move_issue(api_key, issue["id"], state_ids["In Progress"])
            await add_comment(api_key, issue["id"],
                f"Picked up by agentd. Starting triage.")

            pid = _spawn_skill("pickup", iid, None, repo_path, env,
                               context=f"Title: {issue['title']}\n\n{issue.get('description', '')}")
            state.track(issue_id=iid, pid=pid, repo_path=str(repo_path),
                        title=issue["title"], worktree=str(repo_path),
                        linear_issue_id=issue["id"])
            summary["dispatched"] += 1

        # --- Phase 4: Re-spawn for human replies (Blocked, In Review, In Progress) ---
        for linear_state in ["Blocked", "In Review", "In Progress"]:
            for issue in issues_by_state.get(linear_state, []):
                iid = issue["identifier"]
                if state.is_tracked(iid):
                    continue
                labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                if not any(t in labels for t in repo_config.trigger_labels):
                    continue

                reply = await get_latest_human_reply_after_agent(api_key, issue["id"])
                if not reply:
                    continue

                wt = _find_worktree(repo_path, iid)
                if not wt:
                    continue

                # Read handoff to determine the right skill
                handoff = _read_handoff(wt)
                phase = handoff.get("phase", "") if handoff else ""

                if phase == "spec_complete" and "approved" in reply.lower():
                    skill = "implement"
                    if "In Progress" in state_ids:
                        await move_issue(api_key, issue["id"], state_ids["In Progress"])
                    await add_comment(api_key, issue["id"], "Spec approved. Starting implementation.")
                else:
                    skill = "feedback"

                pid = _spawn_skill(skill, iid, wt, repo_path, env,
                                   context=f"Human reply:\n\n{reply}")
                state.track(issue_id=iid, pid=pid, repo_path=str(repo_path),
                            title=issue["title"], worktree=wt,
                            linear_issue_id=issue["id"])
                summary["dispatched"] += 1
                log.info(f"{iid}: human replied in {linear_state} → /{skill}")

    return summary


def run() -> dict:
    return asyncio.run(run_cycle())
