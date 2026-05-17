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


AGENT_PREAMBLE = """## Running unattended (agent-dispatch)

You are running as an automated agent dispatched from a Linear issue.
There is no human at the terminal.

When gstack skills ask you questions (AskUserQuestion):
- Pick the recommended option for routine choices (formatting, naming, style)
- For significant decisions (scope changes, architecture choices, "should we
  also do X?"), STOP and do the following:
  1. Commit any work done so far
  2. Write the question and your recommendation to a file: .dispatch-question.md
  3. Exit cleanly

The dispatch system will post your question to Linear and wait for the
user to reply. You will be resumed with their answer.

Do NOT guess on important decisions. It's better to stop and ask than to
build the wrong thing.

## Progress tracking

Update .dispatch-progress.md in your working directory as you work.
Write a short status after each major step, for example:

```
## Progress
- [x] Read codebase, wrote plan
- [x] Implemented core feature
- [ ] Running /review
- [ ] Push and create PR
```

Keep it short. This file is posted to Linear so the user can see
what you're doing from their phone.
"""


SPEC_PROMPT = """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Your role

You are a principal-level engineer doing design review for this task.
You do NOT implement anything. You produce a spec for a human to review.

## Task
{title}

{body}

## What to produce

Write SPEC.md in this directory with the following structure:

### 1. Problem Analysis
- What is being asked?
- What is the user-facing outcome?
- What are the constraints?

### 2. Scope Assessment
- Is this one task or should it be broken into multiple tickets?
- If multiple: list each sub-ticket as a YAML block (parsed by the dispatch system):

```yaml
split: true
tickets:
  - title: "Short descriptive title"
    description: "What this ticket delivers"
    depends_on: []
  - title: "Second ticket"
    description: "What this delivers"
    depends_on: ["Short descriptive title"]
```

- If one task: write `split: false` and confirm it's appropriately scoped

### 3. Technical Approach
- Which files need to change and why?
- What is the architecture / data flow?
- What are the key design decisions and trade-offs?
- Are there alternative approaches? Why this one?

### 4. Edge Cases & Risks
- What could go wrong?
- What are the edge cases?
- What assumptions are we making?

### 5. Verification Plan

Three levels of verification, each must be specified:

**Level 1: Unit Tests**
- List specific unit tests to write
- What functions/components need test coverage?
- What edge cases must be covered?
- Example test names and what they verify

**Level 2: Integration Tests**
- How do the pieces work together?
- What API contracts need testing?
- What end-to-end flows need coverage?
- Any cross-service or cross-module interactions?

**Level 3: Manual QA (human gate)**
- Step-by-step QA script a human can follow
- What to look at, click, and verify
- Expected vs unexpected behavior
- Specific URLs, pages, or flows to test
- Screenshots or recordings to capture

The agent implements Levels 1 and 2. Level 3 is a checklist for the
human reviewer to follow after the PR is created.

### 6. Implementation Plan
- Ordered list of steps
- Dependencies between steps
- Estimated complexity (trivial / moderate / complex)

## How to work

1. Read the codebase thoroughly. Understand the architecture.
2. Think about this like a principal engineer — not just "how" but "should we"
3. Consider whether the scope is right. A good engineer pushes back on scope
   as often as they accept it.
4. Write SPEC.md with your analysis
5. Exit cleanly

Do NOT write any implementation code. Do NOT create branches or PRs.
Your only output is SPEC.md.

{skills}
"""


IMPLEMENT_PROMPT = """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Approved Spec

The following implementation plan was reviewed and approved by a human engineer.
Follow it closely:

{spec}

## Lifecycle: Implement → Test → Review → Ship

1. git checkout -b {branch}
2. Follow the implementation plan in the approved spec
3. Write the unit tests specified in the Verification Plan (Level 1)
4. Implement the feature, ensuring unit tests pass
5. Write the integration tests specified in the Verification Plan (Level 2)
6. Run all tests: {test_command}
7. Run /review to catch bugs before shipping
8. Fix anything /review finds
9. git push -u origin {branch}
10. gh pr create --title "{title}" --body "$(cat <<'PRBODY'
Fixes {issue_id}

<description of changes>

## Manual QA Checklist (Level 3)

<Copy the Level 3 manual QA steps from the spec here so the reviewer
can follow them when testing the PR>
PRBODY
)"

You MUST push and create a PR. The task is not done until the PR exists.

## Constraints
- Follow the approved spec — don't deviate without good reason
- Tests first: write unit tests BEFORE implementation when possible
- Include the Level 3 manual QA checklist in the PR description
- If you discover something the spec missed, note it in the PR description
- One logical change per commit

{skills}
"""


PROMPT_TEMPLATES = {
    Complexity.TRIVIAL: """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Lifecycle: Implement → Review → Ship

1. git checkout -b {branch}
2. Implement the fix
3. Run tests: {test_command}
4. Commit
5. Run /review to check your work
6. git push -u origin {branch}
7. gh pr create --title "{title}" --body "Fixes {issue_id}"

You MUST push and create a PR. The task is not done until the PR exists.

{skills}
""",
    Complexity.MEDIUM: """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Lifecycle: Plan → Implement → Review → Ship

1. git checkout -b {branch}
2. Read the relevant code. Write a brief plan (what, why, which files, risk).
3. Implement. One logical change per commit.
4. Run tests: {test_command}
5. Run /review to catch bugs before shipping
6. Fix anything /review finds
7. git push -u origin {branch}
8. gh pr create --title "{title}" --body "Fixes {issue_id}\\n\\n<description of changes>"

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

## Lifecycle: Think → Plan → Implement → Review → Ship

**Step 1: Decide if this needs product thinking.**

If the task is vague, ambitious, or describes a new feature/system without
a clear spec (e.g., "build notifications", "add social features", "create
an onboarding flow"), then START with:

1. Run /office-hours — this will challenge your assumptions, reframe the
   problem, and produce a design doc
2. Run /plan-ceo-review — this will pressure-test the scope and find the
   10-star version

Post the resulting plan as a commit (PLAN.md) so it's reviewable.

If the task already has a clear spec (specific files to change, defined
acceptance criteria, obvious implementation), skip to Step 2.

**Step 2: Engineering plan and implementation.**

1. git checkout -b {branch}
2. Run /plan-eng-review or write a detailed plan: architecture, data flow,
   edge cases, test strategy
3. Implement incrementally — commit after each logical step
4. Run tests after each step: {test_command}
5. Run /review to catch bugs, security issues, and completeness gaps
6. Fix anything /review finds
7. Run /ship to push and create the PR (or manually):
   git push -u origin {branch}
   gh pr create --title "{title}" --body "Fixes {issue_id}\\n\\n<description>"

You MUST push and create a PR. The task is not done until the PR exists.

## Constraints
- Break large changes into reviewable commits
- Run the full test suite before creating the PR
- If stuck for >5 minutes on one approach, try a different angle
- If fundamentally blocked, write what you learned and stop

{skills}
""",
}


def _get_skills_text(item: WorkItem) -> str:
    """Get formatted skills text for prompts."""
    packs = discover_skill_packs()
    relevant = get_relevant_skills(packs, item.labels)
    discovered_skills = format_skills_for_prompt(relevant)

    explicit_skills = ""
    if item.repo_config.skills:
        explicit_skills = "\n".join(f"  - /{s}" for s in item.repo_config.skills)

    return discovered_skills or explicit_skills


def build_spec_prompt(item: WorkItem) -> str:
    """Build the spec/planning phase prompt."""
    skills_text = _get_skills_text(item)

    prompt = SPEC_PROMPT.format(
        repo_path=item.repo_config.path,
        title=item.title,
        body=item.body,
        skills=skills_text,
    )

    return AGENT_PREAMBLE + prompt


def build_implement_prompt(item: WorkItem, branch: str, spec: str) -> str:
    """Build the implementation phase prompt with approved spec."""
    skills_text = _get_skills_text(item)

    prompt = IMPLEMENT_PROMPT.format(
        repo_path=item.repo_config.path,
        title=item.title,
        body=item.body,
        branch=branch,
        issue_id=item.id,
        test_command=item.repo_config.test_command or "(no test command configured)",
        spec=spec,
        skills=skills_text,
    )

    return AGENT_PREAMBLE + prompt


def build_prompt(item: WorkItem, branch: str, phase: str = "spec", spec: str = "") -> str:
    """Assemble the prompt for the coding agent based on phase."""
    if phase == "spec":
        return build_spec_prompt(item)
    else:
        return build_implement_prompt(item, branch, spec)


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    import re
    slug = text.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')[:50]


def create_worktree(repo_path: Path, branch: str, issue_id: str, title: str) -> Path:
    """Create a git worktree for isolated agent work. Reuses existing if present."""
    slug = _slugify(title)
    worktree_name = f"{issue_id.lower()}-{slug}"
    worktree_dir = repo_path / "worktrees" / worktree_name

    if worktree_dir.exists():
        return worktree_dir

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # Create branch and worktree in one step
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir)],
        cwd=str(repo_path),
        capture_output=True,
    )

    return worktree_dir


def spawn_agent(item: WorkItem, state: StateStore, phase: str = "spec", spec: str = "") -> dict | None:
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

    # Build the prompt based on phase
    prompt = build_prompt(item, branch, phase=phase, spec=spec)

    # Write prompt to a temp file (avoids shell argument length limits)
    prompt_file = Path(tempfile.mktemp(prefix="dispatch-prompt-", suffix=".md"))
    prompt_file.write_text(prompt)

    # Output file for capturing agent results
    output_file = Path(tempfile.mktemp(prefix="dispatch-output-", suffix=".jsonl"))

    # Resolve full path to agent binary (cron doesn't have PATH)
    import shutil
    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"
    codex_path = shutil.which("codex") or "codex"

    # Spawn based on agent tool — run in the WORKTREE, not the main repo
    if config.agent_tool == "claude":
        cmd = [
            claude_path, "-p",
            "--output-format", "json",
            "--max-turns", "200",
            "--dangerously-skip-permissions",
        ]
    elif config.agent_tool == "codex":
        cmd = [
            codex_path, "--full-auto",
            "--prompt", prompt,
        ]
    else:
        cmd = [claude_path, "-p", "--dangerously-skip-permissions"]

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

    # Read previous attempt count if retrying
    meta_path = Path.home() / ".dispatch" / "runs" / item.id
    attempts_file = meta_path / "attempts"
    prev_attempts = int(attempts_file.read_text().strip()) if attempts_file.exists() else 0

    # Track in state
    state.dispatch(
        item_id=item.id,
        repo_path=str(config.path),
        title=item.title,
        agent_pid=proc.pid,
        branch=branch,
    )
    # Carry forward attempt count
    if prev_attempts > 0:
        state.update_status(item.id, Status.DISPATCHED, attempts=prev_attempts + 1)

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
    except json.JSONDecodeError:
        return {"status": "completed", "pr_url": None, "output": content[:2000]}

    # Check if it's an error result (max turns, permission denied, etc.)
    if data.get("is_error"):
        errors = data.get("errors", [])
        error_msg = "; ".join(errors) if errors else "Unknown error"
        return {"status": "failed", "pr_url": None, "output": error_msg}

    result_text = data.get("result", "")

    # Look for PR URL in the output
    import re
    pr_url = None
    for line in result_text.splitlines():
        if "github.com" in line and "/pull/" in line:
            match = re.search(r'https://github\.com/[^\s)]+/pull/\d+', line)
            if match:
                pr_url = match.group(0)
                break

    status = "completed" if pr_url else "completed"
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
                # Still running — check for question file (agent wants to ask user)
                worktree = _get_worktree_path(item)
                if worktree:
                    question_file = worktree / ".dispatch-question.md"
                    if question_file.exists():
                        question = question_file.read_text().strip()
                        state.update_status(item.id, Status.BLOCKED,
                            pending_question_id=None)
                        updates.append({
                            "id": item.id,
                            "status": "blocked",
                            "question": question,
                        })
                        continue

                    # Check for progress updates
                    progress_file = worktree / ".dispatch-progress.md"
                    if progress_file.exists():
                        progress = progress_file.read_text().strip()
                        # Only report if changed since last check
                        meta_path = Path.home() / ".dispatch" / "runs" / item.id
                        last_progress_file = meta_path / "last_progress"
                        last_progress = last_progress_file.read_text().strip() if last_progress_file.exists() else ""
                        if progress != last_progress:
                            meta_path.mkdir(parents=True, exist_ok=True)
                            last_progress_file.write_text(progress)
                            updates.append({
                                "id": item.id,
                                "status": "progress",
                                "progress": progress,
                            })

                # Check for timeout
                elapsed = time.time() - item.dispatched_at
                if elapsed > 7200:  # 2 hours
                    state.mark_stuck(item.id)
                    updates.append({"id": item.id, "status": "stuck", "reason": "timeout"})
            except ProcessLookupError:
                # Process finished — check for question file first
                worktree = _get_worktree_path(item)
                if worktree:
                    question_file = worktree / ".dispatch-question.md"
                    if question_file.exists():
                        question = question_file.read_text().strip()
                        state.update_status(item.id, Status.BLOCKED,
                            pending_question_id=None)
                        updates.append({
                            "id": item.id,
                            "status": "blocked",
                            "question": question,
                        })
                        continue

                # Read output
                result = read_agent_output(item.id)

                if result.get("status") == "failed":
                    state.mark_failed(item.id, error=result.get("output", "")[:500])
                    updates.append({"id": item.id, "status": "failed"})
                elif item.phase == "spec":
                    # Spec phase: success = SPEC.md exists
                    worktree = _get_worktree_path(item)
                    spec_exists = worktree and (worktree / "SPEC.md").exists()
                    if spec_exists:
                        state.update_status(item.id, Status.DONE)
                        updates.append({"id": item.id, "status": "done"})
                    else:
                        state.mark_failed(item.id, error="Spec phase finished but no SPEC.md written")
                        updates.append({"id": item.id, "status": "failed"})
                elif result.get("pr_url"):
                    state.update_status(item.id, Status.DONE, pr_url=result["pr_url"])
                    updates.append({"id": item.id, "status": "done", "pr_url": result["pr_url"]})
                else:
                    # Implementation finished but no PR found
                    state.update_status(item.id, Status.AUDITING)
                    updates.append({"id": item.id, "status": "auditing"})

    return updates


def respawn_for_review(item_id: str, feedback: str, state: StateStore) -> dict | None:
    """Re-spawn an agent to address PR review feedback in the existing worktree."""
    import shutil

    tracked = state._items.get(item_id)
    if not tracked:
        return None

    worktree = _get_worktree_path(tracked)
    if not worktree:
        return None

    prompt = AGENT_PREAMBLE + f"""You are working in: {worktree}

## PR Review Feedback

Your PR received changes requested. Address this feedback:

{feedback}

## Process
1. Read the feedback carefully
2. Make the requested changes
3. Run tests if available
4. Run /review on your changes
5. Commit and push to the same branch: git push

Do NOT create a new PR. Push to the existing branch and the PR updates automatically.
"""

    prompt_file = Path(tempfile.mktemp(prefix="dispatch-prompt-", suffix=".md"))
    prompt_file.write_text(prompt)

    output_file = Path(tempfile.mktemp(prefix="dispatch-output-", suffix=".jsonl"))

    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"
    cmd = [
        claude_path, "-p",
        "--output-format", "json",
        "--max-turns", "30",
        "--dangerously-skip-permissions",
    ]

    with open(output_file, "w") as out_f:
        with open(prompt_file, "r") as prompt_in:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                stdin=prompt_in,
                stdout=out_f,
                stderr=subprocess.PIPE,
            )

    state.update_status(item_id, Status.WORKING, agent_pid=proc.pid)

    meta_path = Path.home() / ".dispatch" / "runs" / item_id
    meta_path.mkdir(parents=True, exist_ok=True)
    (meta_path / "prompt.md").write_text(prompt)
    (meta_path / "output_file").write_text(str(output_file))

    return {"pid": proc.pid, "worktree": str(worktree)}


def _get_worktree_path(item) -> Path | None:
    """Resolve the worktree path for a tracked item."""
    if not item.repo_path or not item.branch:
        return None
    repo = Path(item.repo_path)
    # Derive worktree dir from the branch name
    # branch is like "agent/bet-5-abc123", worktree is "worktrees/bet-5-<slug>"
    worktrees_dir = repo / "worktrees"
    if not worktrees_dir.exists():
        return None
    # Find the matching worktree
    issue_prefix = item.id.lower()
    for child in worktrees_dir.iterdir():
        if child.is_dir() and child.name.startswith(issue_prefix):
            return child
    return None
