"""Spawn coding agents with assembled context."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .scanner import WorkItem
from .skills import discover_skill_packs, get_all_skills, format_skills_for_prompt
from .state import StateStore, Status

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _load_tool_docs() -> str:
    """Load all tool docs from prompts/tools/."""
    tools_dir = PROMPTS_DIR / "tools"
    parts = []
    if tools_dir.exists():
        for f in sorted(tools_dir.iterdir()):
            if f.suffix == ".md":
                parts.append(f.read_text())
    return "\n\n".join(parts)


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')[:50]


def _spec_filename(issue_id: str, title: str) -> str:
    """Generate spec filename: bet-9-extract-restaurant-name.md"""
    slug = _slugify(title)
    return f"{issue_id.lower()}-{slug}.md"


def find_spec_file(worktree: Path) -> Path | None:
    """Find any spec file in the worktree's specs/ directory."""
    specs_dir = worktree / "specs"
    if not specs_dir.exists():
        legacy = worktree / "SPEC.md"
        return legacy if legacy.exists() else None
    for f in specs_dir.iterdir():
        if f.suffix == ".md":
            return f
    return None


def _get_worktree_path(item) -> Path | None:
    """Resolve the worktree path for a tracked item."""
    if not item.repo_path:
        return None
    repo = Path(item.repo_path)
    worktrees_dir = repo / "worktrees"
    if not worktrees_dir.exists():
        return None
    issue_prefix = item.id.lower()
    for child in worktrees_dir.iterdir():
        if child.is_dir() and child.name.startswith(issue_prefix):
            return child
    return None


def create_worktree(repo_path: Path, branch: str, issue_id: str, title: str) -> Path:
    """Create a git worktree for isolated agent work. Reuses existing if present."""
    slug = _slugify(title)
    worktree_name = f"{issue_id.lower()}-{slug}"
    worktree_dir = repo_path / "worktrees" / worktree_name

    if worktree_dir.exists():
        return worktree_dir

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir)],
        cwd=str(repo_path),
        capture_output=True,
    )
    return worktree_dir


def build_prompt(item: WorkItem, branch: str, user_reply: str = "") -> str:
    """Assemble the full prompt for the agent.

    The prompt includes everything the agent needs:
    - Preamble (unattended rules)
    - Lifecycle (phases, transitions, state file format)
    - Tools (Linear API, GitHub CLI)
    - Methodology (spec or implement, depending on phase)
    - Skills (gstack, modastack — if available)
    - Issue context (title, body, reply if any)
    """
    preamble = _load_prompt("preamble")
    lifecycle = _load_prompt("lifecycle")
    tools = _load_tool_docs()
    spec_methodology = _load_prompt("spec")
    impl_methodology = _load_prompt("implement")

    # Skills
    packs = discover_skill_packs()
    skills_text = format_skills_for_prompt(get_all_skills(packs))
    if not skills_text and item.repo_config.skills:
        skills_text = "\n".join(f"  - /{s}" for s in item.repo_config.skills)

    # Issue context
    issue_context = f"""## Issue: {item.id}
**{item.title}**

{item.body}

Working directory: {item.repo_config.path}
Branch: {branch}
Spec filename: specs/{_spec_filename(item.id, item.title)}
Linear issue ID: {item.linear_issue_id or 'unknown'}
Team key: {item.repo_config.linear_project}
Test command: {item.repo_config.test_command or '(none configured)'}
"""

    if user_reply:
        issue_context += f"\n## User reply (from Linear)\n\n{user_reply}\n"

    # Assemble — agent reads state file to decide which methodology to use
    prompt = f"""{preamble}

{lifecycle}

{tools}

## Methodology: Spec Phase

{spec_methodology.format(
    repo_path=item.repo_config.path,
    title=item.title,
    body=item.body,
    spec_filename=_spec_filename(item.id, item.title),
    skills=skills_text,
)}

## Methodology: Implementation Phase

{impl_methodology.format(
    repo_path=item.repo_config.path,
    title=item.title,
    body=item.body,
    branch=branch,
    issue_id=item.id,
    test_command=item.repo_config.test_command or '(none configured)',
    spec='<read from specs/ directory>',
    skills=skills_text,
)}

{issue_context}
"""
    return prompt


def spawn_agent(item: WorkItem, state: StateStore, user_reply: str = "") -> dict | None:
    """Spawn a coding agent for the work item. Returns {pid, worktree, branch} or None."""
    config = item.repo_config

    # Check parallel limit
    in_flight = state.get_by_repo(str(config.path))
    if len(in_flight) >= config.max_parallel:
        return None

    # Create a unique branch (or reuse existing worktree)
    branch = f"agent/{item.id.lower()}-{uuid.uuid4().hex[:6]}"
    worktree_dir = create_worktree(config.path, branch, item.id, item.title)

    # Ensure .dispatch dir exists in worktree
    dispatch_dir = worktree_dir / ".dispatch"
    dispatch_dir.mkdir(exist_ok=True)

    # Build the prompt
    prompt = build_prompt(item, branch, user_reply=user_reply)

    # Write prompt to temp file
    prompt_file = Path(tempfile.mktemp(prefix="dispatch-prompt-", suffix=".md"))
    prompt_file.write_text(prompt)

    # Output file
    output_file = Path(tempfile.mktemp(prefix="dispatch-output-", suffix=".json"))

    # Save prompt for audit
    (dispatch_dir / "prompt.md").write_text(prompt)

    # Resolve claude binary
    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"

    # Build env
    spawn_env = os.environ.copy()
    spawn_env.setdefault("HOME", str(Path.home()))
    spawn_env.setdefault("USER", os.environ.get("USER", Path.home().name))

    # Pass Linear API key to the agent so it can call the API
    creds = config.get_credentials()
    linear_key = creds.get("linear_api_key", "")
    if linear_key:
        spawn_env["LINEAR_API_KEY"] = linear_key

    cmd = [
        claude_path, "-p",
        "--output-format", "json",
        "--max-turns", "200",
        "--dangerously-skip-permissions",
    ]

    # Spawn
    stderr_log = dispatch_dir / "stderr.log"
    with open(output_file, "w") as out_f, open(stderr_log, "w") as err_f:
        with open(prompt_file, "r") as prompt_in:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree_dir),
                stdin=prompt_in,
                stdout=out_f,
                stderr=err_f,
                env=spawn_env,
            )

    # Track in state (minimal — just PID and identity)
    state.dispatch(
        item_id=item.id,
        repo_path=str(config.path),
        title=item.title,
        agent_pid=proc.pid,
        branch=branch,
    )
    if item.linear_issue_id:
        state.update_status(item.id, Status.DISPATCHED,
            linear_issue_id=item.linear_issue_id)

    # Read previous attempts
    meta_path = Path.home() / ".dispatch" / "runs" / item.id
    attempts_file = meta_path / "attempts"
    if attempts_file.exists():
        prev = int(attempts_file.read_text().strip())
        state.update_status(item.id, Status.DISPATCHED, attempts=prev + 1)

    # Save output file path for later reading
    meta_path.mkdir(parents=True, exist_ok=True)
    (meta_path / "output_file").write_text(str(output_file))

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

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"status": "completed", "pr_url": None, "output": content[:2000]}

    if data.get("is_error"):
        errors = data.get("errors", [])
        error_msg = "; ".join(errors) if errors else "Unknown error"
        return {"status": "failed", "pr_url": None, "output": error_msg}

    result_text = data.get("result", "")

    pr_url = None
    for line in result_text.splitlines():
        if "github.com" in line and "/pull/" in line:
            match = re.search(r'https://github\.com/[^\s)]+/pull/\d+', line)
            if match:
                pr_url = match.group(0)
                break

    return {"status": "completed", "pr_url": pr_url, "output": result_text[:2000]}


def check_processes(state: StateStore) -> list[dict]:
    """Check if dispatched processes are still alive. Returns status updates."""
    updates = []
    for item in state.get_in_flight():
        if not item.agent_pid:
            continue
        try:
            os.kill(item.agent_pid, 0)
            # Still running
        except ProcessLookupError:
            # Process exited — read output
            result = read_agent_output(item.id)
            if result.get("status") == "failed":
                state.mark_failed(item.id, error=result.get("output", "")[:500])
                updates.append({"id": item.id, "status": "failed"})
            else:
                state.update_status(item.id, Status.DONE,
                    pr_url=result.get("pr_url"))
                updates.append({"id": item.id, "status": "done",
                    "pr_url": result.get("pr_url")})

    return updates
