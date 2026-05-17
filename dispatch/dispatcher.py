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

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text()


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

    preamble = _load_prompt("preamble")
    spec_template = _load_prompt("spec")

    prompt = spec_template.format(
        repo_path=item.repo_config.path,
        title=item.title,
        body=item.body,
        skills=skills_text,
        spec_filename=spec_filename,
    )

    return preamble + "\n" + prompt


def build_implement_prompt(item: WorkItem, branch: str, spec: str) -> str:
    """Build the implementation phase prompt with approved spec."""
    skills_text = _get_skills_text(item)

    preamble = _load_prompt("preamble")
    impl_template = _load_prompt("implement")

    prompt = impl_template.format(
        repo_path=item.repo_config.path,
        title=item.title,
        body=item.body,
        branch=branch,
        issue_id=item.id,
        test_command=item.repo_config.test_command or "(no test command configured)",
        spec=spec,
        skills=skills_text,
    )

    return preamble + "\n" + prompt


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

    preamble = _load_prompt("preamble")
    revision_template = _load_prompt("spec-revision")
    prompt = preamble + "\n" + revision_template.format(
        worktree=worktree,
        spec_path=spec_path,
        current_spec=current_spec,
        feedback=feedback,
    )

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

    preamble = _load_prompt("preamble")
    feedback_template = _load_prompt("pr-feedback")
    prompt = preamble + "\n" + feedback_template.format(
        worktree=worktree,
        feedback=feedback,
    )

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
