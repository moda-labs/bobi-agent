"""Spawn coding agents. Minimal — the agent handles its own lifecycle."""

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .scanner import WorkItem
from .skills import discover_skill_packs, get_all_skills, format_skills_for_prompt
from .state import StateStore, Status

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')[:50]


def _load_prompts() -> str:
    """Load and concatenate all prompt files."""
    parts = []
    for name in ["preamble", "lifecycle", "spec", "implement"]:
        f = PROMPTS_DIR / f"{name}.md"
        if f.exists():
            parts.append(f.read_text())

    # Tools
    tools_dir = PROMPTS_DIR / "tools"
    if tools_dir.exists():
        for f in sorted(tools_dir.iterdir()):
            if f.suffix == ".md":
                parts.append(f.read_text())

    return "\n\n---\n\n".join(parts)


def _build_context(item: WorkItem, branch: str, user_reply: str = "") -> str:
    """Build the issue-specific context block."""
    # Skills
    packs = discover_skill_packs()
    skills_text = format_skills_for_prompt(get_all_skills(packs))

    context = f"""## Issue: {item.id}

**{item.title}**

{item.body}

Working directory: {item.repo_config.path}
Branch: {branch}
Linear issue ID: {item.linear_issue_id or 'unknown'}
Team key: {item.repo_config.linear_project}
Test command: {item.repo_config.test_command or '(none configured)'}
"""

    if user_reply:
        context += f"\n## User reply (from Linear)\n\n{user_reply}\n"

    if skills_text:
        context += f"\n{skills_text}\n"

    return context


def _get_or_create_worktree(repo_path: Path, issue_id: str, title: str) -> Path:
    """Get existing worktree or create a new one."""
    slug = _slugify(title)
    worktree_name = f"{issue_id.lower()}-{slug}"
    worktree_dir = repo_path / "worktrees" / worktree_name

    if worktree_dir.exists():
        return worktree_dir

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{issue_id.lower()}-{uuid.uuid4().hex[:6]}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir)],
        cwd=str(repo_path),
        capture_output=True,
    )
    return worktree_dir


def spawn_agent(item: WorkItem, state: StateStore, user_reply: str = "") -> dict | None:
    """Spawn a coding agent. Returns {pid, worktree} or None."""
    config = item.repo_config

    # Parallel limit
    if len(state.get_by_repo(str(config.path))) >= config.max_parallel:
        return None

    # Worktree
    worktree = _get_or_create_worktree(config.path, item.id, item.title)

    # Assemble prompt: prompts + context
    prompts = _load_prompts()
    context = _build_context(item, worktree.name, user_reply)
    full_prompt = f"{prompts}\n\n---\n\n{context}"

    # Write to temp file for stdin
    prompt_file = Path(tempfile.mktemp(suffix=".md"))
    prompt_file.write_text(full_prompt)

    # Output file
    output_file = Path(tempfile.mktemp(suffix=".json"))

    # Env
    spawn_env = os.environ.copy()
    spawn_env.setdefault("HOME", str(Path.home()))
    creds = config.get_credentials()
    if creds.get("linear_api_key"):
        spawn_env["LINEAR_API_KEY"] = creds["linear_api_key"]

    # Spawn
    claude_path = shutil.which("claude") or "/opt/homebrew/bin/claude"
    with open(output_file, "w") as out_f, open(prompt_file, "r") as in_f:
        proc = subprocess.Popen(
            [claude_path, "-p", "--output-format", "json",
             "--max-turns", "200", "--dangerously-skip-permissions"],
            cwd=str(worktree),
            stdin=in_f,
            stdout=out_f,
            stderr=subprocess.PIPE,
            env=spawn_env,
        )

    # Track
    state.dispatch(
        item_id=item.id,
        repo_path=str(config.path),
        title=item.title,
        agent_pid=proc.pid,
        branch=worktree.name,
    )
    if item.linear_issue_id:
        state.update_status(item.id, Status.DISPATCHED,
            linear_issue_id=item.linear_issue_id)

    # Persist output path for later
    meta_dir = Path.home() / ".dispatch" / "runs" / item.id
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "output_file").write_text(str(output_file))

    return {"pid": proc.pid, "worktree": str(worktree)}


def check_processes(state: StateStore) -> list[dict]:
    """Check if processes are alive. Mark done/failed when they exit."""
    updates = []
    for item in state.get_in_flight():
        if not item.agent_pid:
            continue
        try:
            os.kill(item.agent_pid, 0)
        except ProcessLookupError:
            # Read output to check for errors
            meta_dir = Path.home() / ".dispatch" / "runs" / item.id
            output_path = meta_dir / "output_file"
            failed = False

            if output_path.exists():
                import json
                output_file = Path(output_path.read_text().strip())
                if output_file.exists():
                    content = output_file.read_text().strip()
                    if content:
                        try:
                            data = json.loads(content)
                            if data.get("is_error"):
                                failed = True
                                error = "; ".join(data.get("errors", []))
                                state.mark_failed(item.id, error=error[:500])
                        except (json.JSONDecodeError, ValueError):
                            pass

            if not failed:
                state.update_status(item.id, Status.DONE)

            updates.append({"id": item.id, "status": "failed" if failed else "done"})

    return updates
