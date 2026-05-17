"""Spawn coding agents with assembled context."""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import RepoConfig
from .scanner import Complexity, WorkItem
from .skills import discover_skill_packs, get_relevant_skills, format_skills_for_prompt
from .state import StateStore, Status


PROMPT_TEMPLATES = {
    Complexity.TRIVIAL: """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Process
1. Create a new branch: git checkout -b {branch}
2. Make the fix
3. Run tests: {test_command}
4. Commit with a clear message
5. Push the branch: git push -u origin {branch}
6. Create a PR: gh pr create --title "{title}" --body "Fixes {issue_id}"

You MUST push and create a PR. The task is not done until the PR exists.

{skills}
""",
    Complexity.MEDIUM: """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Process
1. Create a new branch: git checkout -b {branch}
2. Read relevant files before modifying
3. Write a brief plan, then implement
4. Run tests: {test_command}
5. Commit with clear messages (one logical change per commit)
6. Push: git push -u origin {branch}
7. Create a PR: gh pr create --title "{title}" --body "Fixes {issue_id}\\n\\n<description of changes>"

You MUST push and create a PR. The task is not done until the PR exists.

## Constraints
- Don't modify unrelated code
- If tests fail, fix them before creating the PR

{skills}
""",
    Complexity.HEAVY: """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Process
1. Create a new branch: git checkout -b {branch}
2. Read CLAUDE.md and understand the project architecture
3. Plan the implementation
4. Implement incrementally — commit after each logical step
5. Run tests after each step: {test_command}
6. Self-review: read your own diff, check for issues
7. Push: git push -u origin {branch}
8. Create a PR with a clear description: gh pr create --title "{title}" --body "Fixes {issue_id}\\n\\n<description>"

You MUST push and create a PR. The task is not done until the PR exists.

## Constraints
- Break large changes into reviewable commits
- Run the full test suite before creating the PR
- If stuck for >5 minutes on one approach, try a different angle
- If fundamentally blocked, write what you learned and stop

{skills}
""",
}


def build_prompt(item: WorkItem, branch: str) -> str:
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

    skills_text = discovered_skills or explicit_skills

    return template.format(
        repo_path=config.path,
        title=item.title,
        body=item.body,
        branch=branch,
        issue_id=item.id,
        test_command=config.test_command or "(no test command configured)",
        skills=skills_text,
    )


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    import re
    slug = text.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')[:50]


def create_worktree(repo_path: Path, branch: str, issue_id: str, title: str) -> Path:
    """Create a git worktree for isolated agent work."""
    slug = _slugify(title)
    worktree_name = f"{issue_id.lower()}-{slug}"
    worktree_dir = repo_path / "worktrees" / worktree_name

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # Create branch and worktree in one step
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir)],
        cwd=str(repo_path),
        capture_output=True,
    )

    return worktree_dir


def spawn_agent(item: WorkItem, state: StateStore) -> dict | None:
    """Spawn a coding agent for the work item. Returns {pid, worktree, branch} or None."""
    config = item.repo_config

    # Check parallel limit
    in_flight = state.get_by_repo(str(config.path))
    if len(in_flight) >= config.max_parallel:
        return None

    # Create a unique branch for this work
    branch = f"agent/{item.id.lower()}-{uuid.uuid4().hex[:6]}"

    # Create an isolated worktree for the agent
    worktree_dir = create_worktree(config.path, branch, item.id, item.title)

    # Build the prompt
    prompt = build_prompt(item, branch)

    # Write prompt to a temp file (avoids shell argument length limits)
    prompt_file = Path(tempfile.mktemp(prefix="dispatch-prompt-", suffix=".md"))
    prompt_file.write_text(prompt)

    # Output file for capturing agent results
    output_file = Path(tempfile.mktemp(prefix="dispatch-output-", suffix=".jsonl"))

    # Spawn based on agent tool — run in the WORKTREE, not the main repo
    if config.agent_tool == "claude":
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--max-turns", "50",
            "--dangerously-skip-permissions",
        ]
    elif config.agent_tool == "codex":
        cmd = [
            "codex", "--full-auto",
            "--prompt", prompt,
        ]
    else:
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]

    # Spawn in background in the worktree
    # Pipe prompt via stdin, capture output to file
    with open(output_file, "w") as out_f:
        with open(prompt_file, "r") as prompt_in:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree_dir),
                stdin=prompt_in,
                stdout=out_f,
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

    # Store metadata for later retrieval
    if item.linear_issue_id:
        state.update_status(item.id, Status.DISPATCHED,
            linear_issue_id=item.linear_issue_id)

    # Store file paths in state for cleanup/reading later
    meta_path = Path.home() / ".dispatch" / "runs" / item.id
    meta_path.mkdir(parents=True, exist_ok=True)
    (meta_path / "prompt.md").write_text(prompt)
    (meta_path / "output_file").write_text(str(output_file))
    (meta_path / "prompt_file").write_text(str(prompt_file))

    return {"pid": proc.pid, "worktree": str(worktree_dir), "branch": branch}


def read_agent_output(item_id: str) -> dict:
    """Read the output from a completed agent run."""
    meta_path = Path.home() / ".dispatch" / "runs" / item_id
    output_path_file = meta_path / "output_file"

    if not output_path_file.exists():
        return {"status": "unknown", "pr_url": None}

    output_file = Path(output_path_file.read_text().strip())
    if not output_file.exists():
        return {"status": "unknown", "pr_url": None}

    content = output_file.read_text().strip()
    if not content:
        return {"status": "no_output", "pr_url": None}

    # Try to parse as JSON (claude --output-format json returns a JSON object)
    try:
        data = json.loads(content)
        result_text = data.get("result", "")
    except json.JSONDecodeError:
        result_text = content

    # Look for PR URL in the output
    pr_url = None
    for line in result_text.splitlines():
        if "github.com" in line and "/pull/" in line:
            # Extract URL
            import re
            match = re.search(r'https://github\.com/[^\s)]+/pull/\d+', line)
            if match:
                pr_url = match.group(0)
                break

    # Check if it looks like it succeeded
    status = "completed"
    if "error" in result_text.lower() and "fix" not in result_text.lower():
        status = "failed"

    return {"status": status, "pr_url": pr_url, "output": result_text[:2000]}


async def check_in_flight(state: StateStore) -> list[dict]:
    """Check status of all in-flight items. Returns status updates."""
    updates = []
    for item in state.get_in_flight():
        if item.status == Status.BLOCKED:
            continue

        if item.agent_pid:
            try:
                os.kill(item.agent_pid, 0)
                # Still running — check for timeout
                elapsed = time.time() - item.dispatched_at
                if elapsed > 7200:  # 2 hours
                    state.mark_stuck(item.id)
                    updates.append({"id": item.id, "status": "stuck", "reason": "timeout"})
            except ProcessLookupError:
                # Process finished — read output
                result = read_agent_output(item.id)

                if result.get("pr_url"):
                    state.update_status(item.id, Status.DONE, pr_url=result["pr_url"])
                    updates.append({"id": item.id, "status": "done", "pr_url": result["pr_url"]})
                elif result.get("status") == "failed":
                    state.mark_failed(item.id, error=result.get("output", "")[:500])
                    updates.append({"id": item.id, "status": "failed"})
                else:
                    # Completed but no PR found — mark auditing for manual check
                    state.update_status(item.id, Status.AUDITING)
                    updates.append({"id": item.id, "status": "auditing"})

    return updates
