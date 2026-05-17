"""Spawn coding agents with assembled context."""

import asyncio
import subprocess
import uuid
from pathlib import Path

from .config import RepoConfig
from .scanner import Complexity, WorkItem
from .skills import discover_skill_packs, get_relevant_skills, format_skills_for_prompt
from .state import StateStore, Status


PROMPT_TEMPLATES = {
    Complexity.TRIVIAL: """Fix this issue in the repo at {repo_path}.

Issue: {title}

{body}

Keep it minimal. One commit. Run tests if a test command exists: {test_command}
""",
    Complexity.MEDIUM: """You are working on repo: {repo_path}

Read CLAUDE.md first for project conventions.

## Task
{title}

{body}

## Process
1. Read relevant files before modifying
2. Write a 5-line plan: what, why, which files, test case, risk
3. Implement the change
4. Run tests: {test_command}
5. Create a commit with a clear message

## Constraints
- One logical change per commit
- Don't modify unrelated code
""",
    Complexity.HEAVY: """You are working on repo: {repo_path}

Read CLAUDE.md first for project conventions.

## Task
{title}

{body}

## Process
1. Read CLAUDE.md and understand the project architecture
2. Plan the implementation (write to PLAN.md if complex)
3. Implement incrementally — commit after each logical step
4. Run tests after each step: {test_command}
5. Self-review: read your own diff, check for issues
6. Create a PR with a clear description

## Constraints
- Break large changes into reviewable commits
- Run the full test suite before creating the PR
- If stuck for >5 minutes on one approach, try a different angle
- If fundamentally blocked, write what you learned and stop

## Available skills (if gstack installed)
{skills}
""",
}


def build_prompt(item: WorkItem) -> str:
    """Assemble the prompt for the coding agent."""
    config = item.repo_config
    template = PROMPT_TEMPLATES[item.complexity]

    # Discover installed skills and filter to relevant ones
    packs = discover_skill_packs()
    relevant = get_relevant_skills(packs, item.labels)
    discovered_skills = format_skills_for_prompt(relevant)

    # Also include explicitly configured skills from .dispatch.yaml
    explicit_skills = ""
    if config.skills:
        explicit_skills = "\n".join(f"  - /{s}" for s in config.skills)

    # Merge: discovered skills take priority, explicit fills gaps
    skills_text = discovered_skills or explicit_skills

    return template.format(
        repo_path=config.path,
        title=item.title,
        body=item.body,
        test_command=config.test_command or "(no test command configured)",
        skills=skills_text,
    )


def spawn_agent(item: WorkItem, state: StateStore) -> int | None:
    """Spawn a coding agent for the work item. Returns PID or None on failure."""
    config = item.repo_config

    # Check parallel limit
    in_flight = state.get_by_repo(str(config.path))
    if len(in_flight) >= config.max_parallel:
        return None

    # Build the prompt
    prompt = build_prompt(item)

    # Create a unique branch for this work
    branch = f"agent/{item.id.lower()}-{uuid.uuid4().hex[:6]}"

    # Spawn based on agent tool
    if config.agent_tool == "claude":
        cmd = [
            "claude", "-p", prompt,
            "--allowedTools", "Bash,Read,Write,Edit",
            "--output-format", "stream-json",
        ]
    elif config.agent_tool == "codex":
        cmd = [
            "codex", "--full-auto",
            "--prompt", prompt,
        ]
    else:
        cmd = ["claude", "-p", prompt]

    # Spawn in background
    proc = subprocess.Popen(
        cmd,
        cwd=str(config.path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Track in state
    state.dispatch(
        item_id=item.id,
        repo_path=str(config.path),
        title=item.title,
        agent_pid=proc.pid,
        branch=branch,
    )

    return proc.pid


async def check_in_flight(state: StateStore) -> list[dict]:
    """Check status of all in-flight items. Returns status updates."""
    updates = []
    for item in state.get_in_flight():
        if item.agent_pid:
            # Check if process is still running
            try:
                import os
                os.kill(item.agent_pid, 0)
                # Still running — check for timeout
                elapsed = asyncio.get_event_loop().time() - item.dispatched_at
                if elapsed > 7200:  # 2 hours
                    state.mark_stuck(item.id)
                    updates.append({"id": item.id, "status": "stuck", "reason": "timeout"})
            except ProcessLookupError:
                # Process finished — check for output
                state.update_status(item.id, Status.AUDITING)
                updates.append({"id": item.id, "status": "auditing"})

    return updates
