"""CLI interface for modastack."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from .config import GlobalConfig, GLOBAL_CONFIG_DIR
from .setup import generate_dispatch_yaml
from .__version__ import __version__

LOG_PATH = GLOBAL_CONFIG_DIR / "modastack.log"
UPDATE_STATE_PATH = GLOBAL_CONFIG_DIR / "update_state.json"
REPO_ROOT = Path(__file__).parent.parent

HOOK_SETTINGS = {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": ".claude/hooks/session-state.sh", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": ".claude/hooks/session-state.sh", "timeout": 5}]}],
}


def install_hooks(target_path: Path) -> list[str]:
    """Install Claude Code hooks for session state tracking.

    Copies the hook script and merges hook config into .claude/settings.json.
    Skips if target is the modastack repo itself.
    Returns list of actions taken.
    """
    actions = []
    repo_root = Path(__file__).parent.parent

    if target_path.resolve() == repo_root.resolve():
        return actions

    # Copy hook script
    hooks_dir = target_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    src_hook = repo_root / ".claude" / "hooks" / "session-state.sh"
    dst_hook = hooks_dir / "session-state.sh"

    if src_hook.exists() and src_hook.resolve() != dst_hook.resolve():
        import shutil
        shutil.copy2(src_hook, dst_hook)
        dst_hook.chmod(0o755)
        actions.append("Installed .claude/hooks/session-state.sh")

    # Merge hooks into settings.json
    settings_path = target_path / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass

    existing_hooks = settings.get("hooks", {})
    changed = False
    for event_name, event_config in HOOK_SETTINGS.items():
        if event_name not in existing_hooks:
            existing_hooks[event_name] = event_config
            changed = True

    if changed:
        settings["hooks"] = existing_hooks
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        actions.append("Configured hooks in .claude/settings.json")

    return actions


@click.group()
@click.version_option(version=__version__, prog_name="modastack")
def main():
    """Modastack — AI engineering manager + engineer team."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_PATH),
        ],
    )


@main.command()
def start():
    """Start modastack. Connects to the centralized event server for webhooks.

    Usage:
        modastack start
    """
    from modastack.manager.events.consumer import run
    run()


@main.command()
@click.argument("text", required=True)
@click.option("--to", default=None, help="Target an engineer by issue ID (e.g. --to AGD-12)")
def message(text, to):
    """Send a message to the manager or an engineer.

    Usage:
        modastack message "what are you working on?"
        modastack message --to AGD-12 "try a different approach"
    """
    if to:
        click.echo(f"Note: engineer sub-agents run autonomously. "
                   f"To redirect {to}, cancel and re-run the phase.")
        return

    from modastack.manager.session import is_alive, inject, detect_state
    if not is_alive():
        click.echo("Manager not running. Start with: modastack start")
        return

    state = detect_state()
    if state != "waiting_input":
        click.echo(f"Manager is busy ({state}). Message queued.")

    ok = inject(text)
    if ok:
        click.echo(f"Sent: {text}")
    else:
        click.echo("Failed to send message.", err=True)


@main.command()
@click.option("-n", "--lines", default=20, help="Number of recent entries to show")
@click.option("-f", "--follow", is_flag=True, help="Follow mode — stream new entries")
def log(lines, follow):
    """Show manager conversation history.

    Usage:
        modastack log              # last 20 entries
        modastack log -n 50        # last 50 entries
        modastack log -f           # follow mode (like tail -f)
    """
    from modastack.manager.session import ACTIVITY_LOG

    if not ACTIVITY_LOG.exists():
        click.echo("No activity yet. Start with: modastack start")
        return

    if follow:
        import time
        shown = set()
        all_lines = ACTIVITY_LOG.read_text().strip().splitlines()
        for line in all_lines[-lines:]:
            _print_activity_entry(line)
            shown.add(line)
        try:
            while True:
                time.sleep(1)
                current = ACTIVITY_LOG.read_text().strip().splitlines()
                for line in current:
                    if line not in shown:
                        _print_activity_entry(line)
                        shown.add(line)
        except KeyboardInterrupt:
            pass
    else:
        all_lines = ACTIVITY_LOG.read_text().strip().splitlines()
        for line in all_lines[-lines:]:
            _print_activity_entry(line)


def _print_activity_entry(line: str) -> None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return

    event = entry.get("event", "")
    ts = entry.get("ts", 0)

    if event == "UserPromptSubmit":
        text = entry.get("text", "")[:120]
        click.echo(f"  → {text}")
    elif event == "response":
        text = entry.get("text", "")[:200]
        click.echo(f"  ← {text}")
    elif event == "Stop":
        session_id = entry.get("session_id", "")[:8]
        click.echo(f"  ◼ turn complete (session {session_id})")


@main.command(hidden=True)
@click.argument("text", required=False)
def tick(text):
    """Deprecated: use 'modastack message' instead."""
    ctx = click.get_current_context()
    if text:
        ctx.invoke(message, text=text)
    else:
        from modastack.manager.session import is_alive, detect_state
        state = detect_state() if is_alive() else "stopped"
        click.echo(f"Manager state: {state}")
        click.echo("Hint: use 'modastack message' to send, 'modastack log' to read.")


@main.command()
def status():
    """Show active agents — manager + engineer sub-agents."""
    from modastack.manager.session import is_alive, detect_state, get_session_id
    from modastack.subagent import list_agents

    mgr_state = detect_state() if is_alive() else "stopped"
    mgr_session = get_session_id()[:8] if is_alive() else ""
    click.echo(f"  Manager: {mgr_state}" + (f" (session {mgr_session})" if mgr_session else ""))

    agents = list_agents()
    if not agents:
        click.echo("  Engineers: none active")
        return

    click.echo(f"  Engineers: {len(agents)} active")
    for agent in agents:
        state = "running" if agent["running"] else "done"
        click.echo(f"    {agent['issue_id']}/{agent['phase']} — {state} ({agent['elapsed_s']}s)")


@main.command()
@click.argument("issue_id", required=False)
@click.option("--cancel", is_flag=True, help="Cancel a running engineer agent")
def engineers(issue_id, cancel):
    """List active engineers, or inspect/cancel a specific one.

    Usage:
        modastack engineers              # list all active
        modastack engineers AGD-12       # show details for AGD-12
        modastack engineers AGD-12 --cancel  # cancel AGD-12
    """
    from modastack.subagent import list_agents, cancel_agent, is_running, get_result

    if issue_id and cancel:
        if cancel_agent(issue_id):
            click.echo(f"Cancelled {issue_id}")
        else:
            click.echo(f"No running agent for {issue_id}")
        return

    if issue_id:
        if is_running(issue_id):
            agents = list_agents()
            for a in agents:
                if a["issue_id"].lower() == issue_id.lower():
                    click.echo(f"  Issue:   {a['issue_id']}")
                    click.echo(f"  Phase:   {a['phase']}")
                    click.echo(f"  Status:  running ({a['elapsed_s']}s)")
                    click.echo(f"  CWD:     {a['cwd']}")
                    return
        result = get_result(issue_id)
        if result:
            click.echo(f"  Issue:   {result.issue_id}")
            click.echo(f"  Phase:   {result.phase}")
            click.echo(f"  Status:  {'success' if result.success else 'failed'}")
            click.echo(f"  Turns:   {result.num_turns}")
            click.echo(f"  Time:    {result.duration_ms / 1000:.1f}s")
            if result.error:
                click.echo(f"  Error:   {result.error}")
        else:
            click.echo(f"No agent found for {issue_id}")
        return

    agents = list_agents()
    if not agents:
        click.echo("No active engineers.")
        return

    for agent in agents:
        state = "running" if agent["running"] else "done"
        click.echo(f"  {agent['issue_id']}/{agent['phase']} — {state} ({agent['elapsed_s']}s)")


@main.command()
@click.option("--tail", default=20, help="Number of recent events to show")
def events(tail):
    """Show recent events from the event bus."""
    events_path = Path.home() / ".modastack" / "manager" / "events.jsonl"
    if not events_path.exists():
        click.echo("No events yet.")
        return

    lines = events_path.read_text().strip().splitlines()
    for line in lines[-tail:]:
        entry = json.loads(line)
        data = entry.get("data", {})
        detail = data.get("text", "") or data.get("title", "") or data.get("issue_id", "")
        if len(detail) > 80:
            detail = detail[:80] + "..."
        click.echo(f"  {entry['timestamp']}  {entry['source']:8s}  {entry['type']}")
        if detail:
            click.echo(f"    {detail}")


@main.command()
def decisions():
    """Show recent manager decisions."""
    decisions_path = Path.home() / ".modastack" / "manager" / "decisions.jsonl"
    if not decisions_path.exists():
        click.echo("No decisions yet.")
        return

    lines = decisions_path.read_text().strip().splitlines()
    for line in lines[-5:]:
        entry = json.loads(line)
        actions = entry.get("actions", [])
        types = ", ".join(a.get("type", "?") for a in actions)
        click.echo(f"  {entry['timestamp']}  {types}")
        if entry.get("reasoning"):
            reason = entry["reasoning"][:200].replace("\n", " ")
            click.echo(f"    {reason}")
        click.echo()


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--task-tracking", type=click.Choice(["github-issues", "linear"]), default=None)
@click.option("--project", default=None, help="Project prefix (e.g., BET, TESS)")
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None)
def register(repo_path: str, task_tracking: str | None, project: str | None, linear_key: str | None):
    """Register a repo with modastack (alias for setup)."""
    from click import Context
    ctx = click.get_current_context()
    ctx.invoke(setup, repo_path=repo_path, task_tracking=task_tracking,
               project=project, linear_key=linear_key, non_interactive=True)


@main.command()
@click.option("--non-interactive", is_flag=True, envvar="CI")
def init(non_interactive):
    """Initialize global config."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")
    click.echo("Run `modastack setup <repo>` to add a repo.")


@main.command()
def repos():
    """List registered repos."""
    config = GlobalConfig.load()
    if not config.repos:
        click.echo("No repos registered.")
        return
    for path in config.repos:
        has_config = (path / ".modastack.yaml").exists()
        click.echo(f"  {path.name:30s} [{'ready' if has_config else 'no config'}] {path}")


EVENT_SERVER_URL = "https://modastack-events.modalabs.workers.dev"


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--task-tracking", type=click.Choice(["github-issues", "linear"]), default=None,
              help="Task tracking system (default: github-issues)")
@click.option("--project", default=None, help="Project prefix (e.g., BET, TESS)")
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None, help="Linear API key (only for --task-tracking linear)")
@click.option("--non-interactive", is_flag=True, envvar="CI")
def setup(repo_path: str, task_tracking: str | None, project: str | None,
          linear_key: str | None, non_interactive: bool):
    """Set up a repo for modastack."""
    import yaml

    path = Path(repo_path).resolve()
    config_path = path / ".modastack.yaml"
    credential_name = path.name

    if config_path.exists() and not non_interactive:
        try:
            if not click.confirm(f".modastack.yaml exists in {path}. Overwrite?"):
                return
        except (EOFError, click.Abort):
            pass

    # Default to github-issues
    if not task_tracking:
        task_tracking = "linear" if linear_key else "github-issues"

    # Handle credentials for Linear
    if task_tracking == "linear":
        from .config import Credentials
        creds = Credentials.load()
        existing_cred = creds.get(credential_name)
        has_key = bool(existing_cred.get("linear_api_key"))

        if linear_key:
            creds.add(credential_name, linear_api_key=linear_key)
            click.echo(f"Linear API key stored for '{credential_name}'")
        elif not has_key and not non_interactive:
            try:
                key = click.prompt("Linear API key", default="", show_default=False)
                if key:
                    creds.add(credential_name, linear_api_key=key)
            except (EOFError, click.Abort):
                pass
        elif has_key:
            click.echo(f"Linear API key already configured for '{credential_name}'")

    config = generate_dispatch_yaml(path, task_tracking=task_tracking)
    config["credentials"] = credential_name
    if project:
        config["task_tracking"]["project"] = project

    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    click.echo(f"Generated: {config_path}")

    # Auto-register in global config
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo("Registered.")

    # Bootstrap task tracker
    if task_tracking == "linear":
        resolved_key = linear_key
        if not resolved_key:
            from .config import Credentials
            resolved_key = (Credentials.load().get(credential_name) or {}).get("linear_api_key")
        resolved_project = project or config["task_tracking"]["project"]
        if resolved_key and resolved_project:
            click.echo("Bootstrapping Linear board...")
            from .board_setup import bootstrap_board
            for action in bootstrap_board(resolved_key, resolved_project):
                click.echo(f"  {action}")
    elif task_tracking == "github-issues":
        click.echo("Bootstrapping GitHub Issues labels...")
        from .github_issues import bootstrap_labels
        for action in bootstrap_labels(path):
            click.echo(f"  {action}")

    # GitHub App: check installation and prompt if missing
    if task_tracking == "github-issues":
        _ensure_github_app(path, non_interactive)

    # Event server: register deployment and subscribe to this repo
    _ensure_event_server(path, global_config)

    # Add .modastack/ to .gitignore
    gitignore_path = path / ".gitignore"
    gitignore_entries = [".modastack/", "worktrees/"]
    existing = gitignore_path.read_text() if gitignore_path.exists() else ""
    added = []
    for entry in gitignore_entries:
        if entry not in existing:
            added.append(entry)
    if added:
        with open(gitignore_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(added) + "\n")
        click.echo(f"Added to .gitignore: {', '.join(added)}")

    # Install skills
    click.echo("Installing skills...")
    repo_root = Path(__file__).parent.parent
    target_skills = path / ".claude" / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    installed = []
    skill_dirs = [
        repo_root / "roles" / "engineer" / "process",
        repo_root / "roles" / "engineer" / "practices",
        repo_root / "roles" / "product_manager",
        repo_root / "roles" / "tools",
    ]
    for category_dir in skill_dirs:
        if not category_dir.exists():
            continue
        for skill_dir in category_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                link = target_skills / skill_dir.name
                if link.exists() or link.is_symlink():
                    continue
                link.symlink_to(os.path.relpath(skill_dir.resolve(), target_skills))
                installed.append(skill_dir.name)
    if installed:
        for name in sorted(installed):
            click.echo(f"  Linked /{name}")
    else:
        click.echo("  Skills already installed.")

    # Install hooks
    hook_actions = install_hooks(path)
    for action in hook_actions:
        click.echo(f"  {action}")

    click.echo(f"Ready — {path.name} is set up for modastack.")


def _get_repo_full_name(path: Path) -> str:
    """Get owner/repo from git remote."""
    if not path.exists():
        return ""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=path,
    )
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    # Handle SSH (git@github.com:owner/repo.git) and HTTPS
    if ":" in url and "@" in url:
        path_part = url.split(":")[-1]
    else:
        path_part = "/".join(url.split("/")[-2:])
    return path_part.removesuffix(".git")


def _ensure_github_app(path: Path, non_interactive: bool) -> None:
    """Check if the Modastack GitHub App is installed on this repo's org."""
    repo_full = _get_repo_full_name(path)
    if not repo_full:
        click.echo("  Could not detect GitHub remote — skipping app check")
        return

    owner = repo_full.split("/")[0]
    result = subprocess.run(
        ["gh", "api", f"orgs/{owner}/installations", "--jq",
         '[.installations[] | select(.app_slug == "modastack")] | length'],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip() not in ("", "0"):
        click.echo(f"  GitHub App installed on {owner}")
        return

    click.echo(f"  GitHub App not installed on {owner}")
    install_url = "https://github.com/apps/modastack/installations/new"
    if not non_interactive:
        click.echo(f"  Install at: {install_url}")
        try:
            if click.confirm("  Open in browser?", default=True):
                subprocess.run(["open", install_url], capture_output=True)
                click.pause("  Press Enter after installing...")
        except (EOFError, click.Abort):
            pass
    else:
        click.echo(f"  Install at: {install_url}")


def _ensure_event_server(path: Path, global_config: GlobalConfig) -> None:
    """Register deployment with the event server if not already configured."""
    import httpx

    if global_config.event_server_deployment_id and global_config.event_server_api_key:
        repo_full = _get_repo_full_name(path)
        if not repo_full:
            return
        try:
            resp = httpx.put(
                f"{global_config.event_server_url}/deployments/{global_config.event_server_deployment_id}/subscriptions",
                json={"add": [repo_full]},
                headers={"Authorization": f"Bearer {global_config.event_server_api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                click.echo(f"  Event server: subscribed to {repo_full} ({len(data['subscriptions'])} total)")
            else:
                click.echo(f"  Event server: failed to add subscription ({resp.status_code})")
        except Exception as e:
            click.echo(f"  Event server: failed to add subscription ({e})")
        return

    # First-time registration
    repo_full = _get_repo_full_name(path)
    all_repos = []
    for repo_path in global_config.repos:
        name = _get_repo_full_name(repo_path)
        if name:
            all_repos.append(name)
    if repo_full and repo_full not in all_repos:
        all_repos.append(repo_full)

    if not all_repos:
        click.echo("  No GitHub repos detected — skipping event server registration")
        return

    click.echo(f"  Registering with event server...")
    import socket
    hostname = socket.gethostname()
    try:
        resp = httpx.post(
            f"{EVENT_SERVER_URL}/deployments",
            json={"name": hostname, "subscriptions": all_repos},
            timeout=10,
        )
        if resp.status_code == 201:
            data = resp.json()
            global_config.event_server_url = EVENT_SERVER_URL
            global_config.event_server_deployment_id = data["deployment_id"]
            global_config.event_server_api_key = data["api_key"]
            global_config.save()
            click.echo(f"  Event server: registered ({len(all_repos)} repos)")
        else:
            click.echo(f"  Event server registration failed: {resp.status_code} {resp.text}")
    except Exception as e:
        click.echo(f"  Event server registration failed: {e}")


@main.command()
@click.option("--port", default=8095, help="Dashboard server port")
def dashboard(port):
    """Start the web dashboard."""
    from dashboard.app import run_dashboard
    run_dashboard(port=port)


@main.group()
def history():
    """Conversation history — index and search Claude Code sessions."""
    pass


@history.command("index")
@click.option("--project", default=None, help="Filter to project (substring match on path)")
def history_index(project):
    """Index conversation JSONL files into searchable SQLite.

    Scans ~/.claude/projects/*/conversations/ for JSONL files and indexes
    messages into a local SQLite database for fast searching.

    Usage:
        modastack history index                # index all projects
        modastack history index --project foo   # index only projects matching "foo"
    """
    from .history import index as do_index
    click.echo("Indexing conversations...")
    stats = do_index(project_filter=project)
    click.echo(f"  Scanned {stats['files_scanned']} files, {stats['files_with_new']} had new data")
    click.echo(f"  Indexed {stats['new_messages']} new messages")
    click.echo(f"  Total: {stats['total_conversations']} conversations, {stats['total_messages']} messages")


@history.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Max results")
@click.option("--project", default=None, help="Filter to project")
def history_search(query, limit, project):
    """Full-text search across indexed conversation history.

    Searches message content using SQLite FTS. Requires `modastack history index`
    to have been run first.

    Usage:
        modastack history search "error handling"
        modastack history search "deploy" --project modastack --limit 5
    """
    from .history import search as do_search
    results = do_search(query, limit=limit, project=project)
    if not results:
        click.echo("No results. Run `modastack history index` first.")
        return
    for r in results:
        branch = r.get("git_branch") or ""
        role = r.get("role") or r.get("type") or ""
        tool = f" [{r['tool_name']}]" if r.get("tool_name") else ""
        snippet = (r.get("snippet") or "")[:200].replace("\n", " ")
        click.echo(f"  {r['timestamp'][:19]}  {role:10s}{tool}  {branch}")
        click.echo(f"    {snippet}")
        click.echo()


@history.command("sessions")
@click.option("--limit", default=20)
@click.option("--project", default=None)
def history_sessions(limit, project):
    """List indexed conversations with metadata.

    Shows session ID, git branch, message count, and working directory for
    each indexed conversation. Use session IDs with `modastack history show`.

    Usage:
        modastack history sessions
        modastack history sessions --limit 5 --project modastack
    """
    from .history import conversations
    convos = conversations(limit=limit, project=project)
    if not convos:
        click.echo("No conversations indexed. Run `modastack history index` first.")
        return
    for c in convos:
        branch = c.get("git_branch") or ""
        click.echo(f"  {c['started_at'][:19]}  {c['session_id'][:8]}  {branch:20s}  {c['message_count']} msgs  {c.get('cwd', '')}")


@history.command("show")
@click.argument("session_id")
@click.option("--limit", default=50)
def history_show(session_id, limit):
    """Show messages from a specific session.

    Accepts a full or partial session ID (prefix match). Use
    `modastack history sessions` to find session IDs.

    Usage:
        modastack history show abc12345
        modastack history show abc12345 --limit 10
    """
    from .history import session_messages, conversations
    convos = conversations(limit=1000)
    match = [c for c in convos if c["session_id"].startswith(session_id)]
    if not match:
        click.echo(f"No session matching '{session_id}'")
        return
    full_id = match[0]["session_id"]
    msgs = session_messages(full_id)
    for m in msgs[:limit]:
        role = m.get("role") or m.get("type") or ""
        tool = f" [{m['tool_name']}]" if m.get("tool_name") else ""
        text = (m.get("content") or "")[:300].replace("\n", " ")
        click.echo(f"  {role:10s}{tool}  {text}")


main.add_command(history)


@main.group()
def workflow():
    """Workflow engine — manage YAML-based DAG workflows."""
    pass


@workflow.command("list")
def workflow_list():
    """List available workflow definitions from all sources.

    Scans three tiers in priority order:
      1. Repo-local: <repo>/.modastack/workflows/
      2. User: ~/.modastack/workflows/
      3. Built-in: <modastack>/workflows/

    Usage:
        modastack workflow list
    """
    from .workflow.schema import load_workflow
    from .workflow.triggers import WORKFLOWS_DIR, USER_WORKFLOWS_DIR

    sources = []

    # Repo-specific
    config = GlobalConfig.load()
    for repo_path in config.repos:
        repo_wf_dir = repo_path / ".modastack" / "workflows"
        if repo_wf_dir.exists():
            sources.append((repo_wf_dir, f"repo:{repo_path.name}"))

    # User overrides
    if USER_WORKFLOWS_DIR.exists():
        sources.append((USER_WORKFLOWS_DIR, "user"))

    # Built-in defaults
    sources.append((WORKFLOWS_DIR, "default"))

    found = False
    for directory, source in sources:
        if not directory.exists():
            continue
        for f in sorted(directory.glob("*.yaml")):
            found = True
            try:
                wf = load_workflow(f)
                filters = ", ".join(f"{k}={v}" for k, v in wf.trigger.filter.items())
                filter_str = f" [{filters}]" if filters else ""
                click.echo(f"  {wf.name:25s} {source:15s} trigger={wf.trigger.event}{filter_str}  "
                          f"nodes={len(wf.nodes)}")
            except Exception as e:
                click.echo(f"  {f.name:25s} {source:15s} ERROR: {e}")

    if not found:
        click.echo("No workflows found.")


@workflow.command("status")
def workflow_status():
    """Show active and recent workflow runs.

    Displays up to 20 recent runs with their status, trigger issue,
    node completion progress, and start time.

    Usage:
        modastack workflow status
    """
    from .workflow.state import WorkflowRun
    runs = WorkflowRun.list_runs()
    if not runs:
        click.echo("No workflow runs found.")
        return
    for run in runs[:20]:
        event_data = run.trigger_event.get("data", {})
        issue = event_data.get("issue_id", "?")
        completed = sum(1 for ns in run.nodes.values() if ns.status == "completed")
        total = len(run.nodes)
        click.echo(f"  {run.run_id}  {run.workflow_name:20s} {run.status:10s} "
                  f"issue={issue}  {completed}/{total} nodes  {run.started_at[:19]}")


@workflow.command("validate")
@click.argument("path", type=click.Path(exists=True))
def workflow_validate(path):
    """Validate a workflow YAML file.

    Parses the YAML, checks the DAG structure, reports variable scopes used,
    and prints the topological execution order if valid.

    Usage:
        modastack workflow validate workflows/deploy.yaml
        modastack workflow validate myrepo/.modastack/workflows/deploy.yaml
    """
    import re
    from .workflow.schema import load_workflow
    try:
        wf = load_workflow(Path(path))
        order = wf.topological_order()
        click.echo(f"Valid: {wf.name} v{wf.version} ({len(wf.nodes)} nodes)")
        click.echo(f"Trigger: {wf.trigger.event}")
        if wf.trigger.filter:
            click.echo(f"Filter: {wf.trigger.filter}")
        click.echo(f"Execution order: {' -> '.join(order)}")

        # Report variable scopes referenced
        raw = Path(path).read_text()
        refs = set(re.findall(r'\$\{\{(\w+)\.', raw))
        builtin_scopes = {"event", "config", "repo", "handoff"} | set(wf.nodes.keys())
        unknown = refs - builtin_scopes
        click.echo(f"Variable scopes: {', '.join(sorted(refs))}")
        if unknown:
            click.echo(f"Warning: unknown scopes (may be node outputs): {', '.join(sorted(unknown))}")

        # Show node types breakdown
        from collections import Counter
        type_counts = Counter(n.type.value for n in wf.nodes.values())
        click.echo(f"Node types: {', '.join(f'{t}={c}' for t, c in sorted(type_counts.items()))}")

    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)


main.add_command(workflow)


@main.command("self-update")
def self_update():
    """Pull latest from origin/main and reinstall modastack."""
    log = logging.getLogger(__name__)

    old_version = (REPO_ROOT / "VERSION").read_text().strip()
    click.echo(f"Current version: {old_version}")

    # Fetch latest
    click.echo("Fetching origin/main...")
    result = subprocess.run(
        ["git", "fetch", "origin", "main", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Failed to fetch: {result.stderr.strip()}", err=True)
        sys.exit(1)

    # Check remote version
    result = subprocess.run(
        ["git", "show", "origin/main:VERSION"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo("Failed to read remote VERSION", err=True)
        sys.exit(1)
    remote_version = result.stdout.strip()

    if remote_version == old_version:
        click.echo("Already up to date.")
        return

    # Check for dirty working tree
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()

    stashed = False
    if dirty:
        click.echo("Working tree has uncommitted changes — stashing...")
        subprocess.run(
            ["git", "stash", "push", "-m", "modastack-self-update-backup"],
            cwd=REPO_ROOT, check=True,
        )
        stashed = True

    # Save rollback state
    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()

    import datetime
    UPDATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_STATE_PATH.write_text(json.dumps({
        "pre_update_head": pre_head,
        "pre_update_version": old_version,
        "updated_at": datetime.datetime.now().isoformat(),
        "stashed": stashed,
    }))

    # Pull
    click.echo(f"Updating {old_version} → {remote_version}...")
    result = subprocess.run(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Pull failed (history diverged?): {result.stderr.strip()}", err=True)
        click.echo("Run `modastack rollback` to restore, or reconcile manually.")
        if stashed:
            subprocess.run(["git", "stash", "pop"], cwd=REPO_ROOT)
        sys.exit(1)

    # Reinstall
    click.echo("Reinstalling...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"pip install failed: {result.stderr.strip()}", err=True)
        click.echo("Run `modastack rollback` to restore.")
        sys.exit(1)

    # Pop stash if needed
    if stashed:
        click.echo("Restoring stashed changes...")
        subprocess.run(["git", "stash", "pop"], cwd=REPO_ROOT)

    new_version = (REPO_ROOT / "VERSION").read_text().strip()
    click.echo(f"Updated to v{new_version}")
    log.info(f"Self-update complete: {old_version} → {new_version}")


@main.command()
def rollback():
    """Roll back the last self-update."""
    if not UPDATE_STATE_PATH.exists():
        click.echo("No update state found — nothing to roll back.")
        sys.exit(1)

    state = json.loads(UPDATE_STATE_PATH.read_text())
    pre_head = state["pre_update_head"]
    pre_version = state["pre_update_version"]

    click.echo(f"Rolling back to v{pre_version} (commit {pre_head[:8]})...")

    result = subprocess.run(
        ["git", "reset", "--hard", pre_head],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Reset failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"pip install failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    UPDATE_STATE_PATH.unlink(missing_ok=True)
    click.echo(f"Rolled back to v{pre_version}")


if __name__ == "__main__":
    main()
