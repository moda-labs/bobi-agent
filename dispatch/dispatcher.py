"""Spawn coding agents with assembled context."""

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .scanner import WorkItem
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

You are a principal-level engineer doing design review. You classify,
scope, plan, and route. You do NOT implement anything.

## Task
{title}

{body}

## Process: /frontdoor methodology

Follow the /frontdoor intake process:

### Step 1 — Classify
- **Bug** — broken, regressing, or failing. Past-tense.
- **Inquiry** — question or exploration, no code change implied.
- **Update** — new or changed capability. Future-tense.

If Bug: note it and write a spec focused on investigation + fix.
If Inquiry: write a short answer in specs/{spec_filename} and stop.
If Update: continue with full spec.

### Step 2 — Problem & solution read-back
State back in plain prose:
- The problem this solves, and the user/moment it solves it for
- Your one-sentence read of the proposed solution
- What is explicitly OUT of scope

### Step 3 — Scope guards
Check for: billing/payment primitives, multi-screen user flows,
schema changes on production tables. Note any that apply with your
assessment.

### Step 4 — Size verdict
- **Small** — one cohesive PR, single domain
- **Medium** — one PR, multi-domain
- **Large** — propose a carve into 2-4 tickets

If Large, include a ticket breakdown as YAML:

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

If Small or Medium: write `split: false`.

### Step 5 — Technical Approach
- Which files need to change and why?
- Architecture / data flow (ASCII diagram if helpful)
- Key design decisions and trade-offs
- Alternative approaches considered

### Step 6 — Verification Plan

Three levels, all required:

**Level 1: Unit Tests**
- Specific test names and what they verify
- Edge cases to cover

**Level 2: Integration Tests**
- End-to-end flows, API contracts, cross-module interactions

**Level 3: Manual QA (human gate)**
- Step-by-step script a human follows to verify
- What to look at, click, and check
- Specific URLs, pages, or flows to test

The agent writes Levels 1 and 2. Level 3 goes in the PR for the human reviewer.

### Step 7 — Implementation Plan
- Ordered list of steps with dependencies
- Estimated complexity (trivial / moderate / complex)

## Output

Write specs/{spec_filename} with all of the above. Create the specs/ directory if needed.

Do NOT write any implementation code. Do NOT create branches or PRs.
Your only output is specs/{spec_filename}

{skills}
"""


IMPLEMENT_PROMPT = """You are working in: {repo_path}

Read CLAUDE.md first if it exists.

## Task
{title}

{body}

## Approved Spec

The following spec was reviewed and approved. Follow it closely:

{spec}

## Your role: /build staff-engineer mode

You are a staff engineer shipping production-quality code. Follow the
/build methodology:

1. **Read before you write.** Understand the full system first.
2. **Simple > clever.** Boring, obvious implementations.
3. **Match the codebase.** Your code should look native.
4. **Ship the whole thing.** No TODOs, no stubs, no "implement later."
5. **Tests are not optional.** Every codepath gets a test.

## Lifecycle

1. git checkout -b {branch}
2. Write the Level 1 unit tests from the spec's Verification Plan
3. Implement the feature, ensuring tests pass
4. Write the Level 2 integration tests
5. Run all tests: {test_command}
6. Run /review to catch bugs before shipping
7. Fix anything /review finds
8. git push -u origin {branch}
9. Create the PR with the Level 3 QA checklist in the body:
   gh pr create --title "{title}" --body "Fixes {issue_id}\\n\\n<description>\\n\\n## Manual QA Checklist\\n<Level 3 steps from spec>"

You MUST push and create a PR. The task is not done until the PR exists.

## Constraints
- Follow the approved spec — don't deviate without good reason
- Tests first: write unit tests BEFORE implementation
- Include the Level 3 manual QA checklist in the PR description
- If you discover something the spec missed, note it in the PR
- One logical change per commit
- No `any` types, no empty catches, no console.log in production code
- Use CLI tools for dependencies/config/migrations (never hand-edit manifests)

{skills}
"""


def _get_skills_text(item: WorkItem) -> str:
    """Get formatted skills text for prompts."""
    packs = discover_skill_packs()
    relevant = get_relevant_skills(packs, item.labels)
    discovered_skills = format_skills_for_prompt(relevant)

    explicit_skills = ""
    if item.repo_config.skills:
        explicit_skills = "\n".join(f"  - /{s}" for s in item.repo_config.skills)

    return discovered_skills or explicit_skills


def _spec_filename(issue_id: str, title: str) -> str:
    """Generate spec filename: bet-9-extract-restaurant-name.md"""
    slug = _slugify(title)
    return f"{issue_id.lower()}-{slug}.md"


def get_spec_path(worktree: Path, item_id: str, title: str) -> Path:
    """Get the spec file path for an item."""
    return worktree / "specs" / _spec_filename(item_id, title)


def find_spec_file(worktree: Path) -> Path | None:
    """Find any spec file in the worktree's specs/ directory."""
    specs_dir = worktree / "specs"
    if not specs_dir.exists():
        # Fall back to SPEC.md for backward compat
        legacy = worktree / "SPEC.md"
        return legacy if legacy.exists() else None
    for f in specs_dir.iterdir():
        if f.suffix == ".md":
            return f
    return None


def build_spec_prompt(item: WorkItem) -> str:
    """Build the spec/planning phase prompt."""
    skills_text = _get_skills_text(item)
    spec_filename = _spec_filename(item.id, item.title)

    prompt = SPEC_PROMPT.format(
        repo_path=item.repo_config.path,
        title=item.title,
        body=item.body,
        skills=skills_text,
        spec_filename=spec_filename,
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

    # Resolve full path to agent binary
    import shutil
    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"
    codex_path = shutil.which("codex") or "codex"

    # Build env — ensure HOME and PATH are set for Keychain + tool access
    spawn_env = os.environ.copy()
    spawn_env.setdefault("HOME", str(Path.home()))
    # Claude needs USER for some auth flows
    spawn_env.setdefault("USER", os.environ.get("USER", Path.home().name))
    # Ensure temp dir is accessible
    spawn_env.setdefault("TMPDIR", "/tmp")

    # Log file for child stderr (debugging auth issues)
    child_stderr_path = Path.home() / ".dispatch" / "runs" / item.id / "stderr.log"
    child_stderr_path.parent.mkdir(parents=True, exist_ok=True)

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
    # Pipe prompt via stdin, capture output to file, stderr to log
    with open(output_file, "w") as out_f, open(child_stderr_path, "w") as err_f:
        with open(prompt_file, "r") as prompt_in:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree_dir),
                stdin=prompt_in,
                stdout=out_f,
                stderr=err_f,
                env=spawn_env,
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
                    # Spec phase: success = spec file exists
                    worktree = _get_worktree_path(item)
                    spec_file = find_spec_file(worktree) if worktree else None
                    if spec_file:
                        state.update_status(item.id, Status.DONE)
                        updates.append({"id": item.id, "status": "done"})
                    else:
                        state.mark_failed(item.id, error="Spec phase finished but no spec file written")
                        updates.append({"id": item.id, "status": "failed"})
                elif result.get("pr_url"):
                    state.update_status(item.id, Status.DONE, pr_url=result["pr_url"])
                    updates.append({"id": item.id, "status": "done", "pr_url": result["pr_url"]})
                else:
                    # Implementation finished but no PR found
                    state.update_status(item.id, Status.AUDITING)
                    updates.append({"id": item.id, "status": "auditing"})

    return updates


def _spawn_claude_in_worktree(
    item_id: str,
    prompt: str,
    worktree: Path,
    max_turns: int = 30,
) -> int:
    """Spawn a Claude agent in a worktree. Returns the process PID.

    Shared helper for respawn_for_spec_revision and respawn_for_review.
    Handles temp files, env setup, Popen, and metadata persistence.
    """
    import shutil

    prompt_file = Path(tempfile.mktemp(prefix="dispatch-prompt-", suffix=".md"))
    prompt_file.write_text(prompt)

    output_file = Path(tempfile.mktemp(prefix="dispatch-output-", suffix=".jsonl"))

    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"
    cmd = [
        claude_path, "-p",
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
    ]

    spawn_env = os.environ.copy()
    spawn_env.setdefault("HOME", str(Path.home()))

    with open(output_file, "w") as out_f:
        with open(prompt_file, "r") as prompt_in:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                stdin=prompt_in,
                stdout=out_f,
                stderr=subprocess.PIPE,
                env=spawn_env,
            )

    meta_path = Path.home() / ".dispatch" / "runs" / item_id
    meta_path.mkdir(parents=True, exist_ok=True)
    (meta_path / "prompt.md").write_text(prompt)
    (meta_path / "output_file").write_text(str(output_file))

    return proc.pid


def respawn_for_spec_revision(item_id: str, feedback: str, state: StateStore) -> dict | None:
    """Re-spawn the spec agent to revise SPEC.md based on review feedback."""
    tracked = state._items.get(item_id)
    if not tracked:
        return None

    worktree = _get_worktree_path(tracked)
    if not worktree:
        return None

    # Read current spec so agent has context
    spec_file = find_spec_file(worktree)
    current_spec = spec_file.read_text() if spec_file else ""
    spec_path = spec_file.relative_to(worktree) if spec_file else "specs/spec.md"

    prompt = AGENT_PREAMBLE + f"""You are working in: {worktree}

## Spec Revision

Your spec received feedback during design review. Revise it.

## Current spec ({spec_path})

{current_spec}

## Review Feedback

{feedback}

## Process

1. Read the feedback carefully
2. Revise {spec_path} to address every point
3. If feedback challenges your approach, explain your reasoning OR change it
4. Commit and push

Do NOT implement any code. Only revise the spec file.
"""

    pid = _spawn_claude_in_worktree(item_id, prompt, worktree)
    state.update_status(item_id, Status.WORKING, agent_pid=pid, phase="spec")

    return {"pid": pid, "worktree": str(worktree)}


def respawn_for_review(item_id: str, feedback: str, state: StateStore) -> dict | None:
    """Re-spawn an agent to address PR review feedback in the existing worktree."""
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

    pid = _spawn_claude_in_worktree(item_id, prompt, worktree)
    state.update_status(item_id, Status.WORKING, agent_pid=pid)

    return {"pid": pid, "worktree": str(worktree)}


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
