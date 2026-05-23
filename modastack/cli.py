"""CLI interface for modabot."""

import json
import logging
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


@click.group()
@click.version_option(version=__version__, prog_name="modastack")
def main():
    """Modabot — AI engineering manager + engineer team."""
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
@click.option("--webhooks", is_flag=True, help="Enable webhook server for GitHub/Linear")
@click.option("--port", default=8080, help="Webhook server port")
@click.option("--batch-window", default=5.0, help="Seconds to batch events before processing")
def start(webhooks, port, batch_window):
    """Start modabot. Event-driven — reacts to webhooks and polls.

    Usage:
        modastack start                    # polling mode (default)
        modastack start --webhooks         # webhook + polling mode
        modastack start --webhooks --port 9090
    """
    from manager.events.consumer import run
    run(webhook_port=port, use_webhooks=webhooks, batch_window=batch_window)


@main.command()
@click.argument("message", required=False)
def tick(message):
    """Inject a message into the manager session (for debugging).

    Usage:
        modastack tick                          # check if manager is alive
        modastack tick "what are you working on?"  # ask the manager something
    """
    from manager.session import is_alive, inject, capture, detect_state
    if not is_alive():
        click.echo("Manager session not running. Start with: modastack start")
        return

    state = detect_state()
    click.echo(f"Manager state: {state}")

    if message:
        inject(message)
        click.echo(f"Injected: {message}")
    else:
        pane = capture(lines=10)
        content = [l.strip() for l in pane.splitlines() if l.strip() and "─" not in l and "bypass" not in l]
        for line in content[-5:]:
            click.echo(f"  {line}")


@main.command()
def status():
    """Show active sessions — discovered from tmux, not state files."""
    import subprocess
    import shutil

    tmux = shutil.which("tmux") or "tmux"
    result = subprocess.run(
        [tmux, "list-sessions", "-F", "#{session_name} #{session_created}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo("No tmux sessions running.")
        return

    sessions = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if parts[0] != "moda-manager":
            sessions.append(parts[0])

    if not sessions:
        click.echo("No active engineers.")
        click.echo("")
        # Check manager
        if any("moda-manager" in l for l in result.stdout.splitlines()):
            click.echo("  Manager: running (tmux attach -t moda-manager)")
        return

    for name in sessions:
        # Capture last few lines to show what it's doing
        pane = subprocess.run(
            [tmux, "capture-pane", "-t", name, "-p", "-S", "-3"],
            capture_output=True, text=True,
        ).stdout
        last_line = ""
        for l in reversed(pane.splitlines()):
            l = l.strip()
            if l and "─" not in l and "bypass" not in l and "⏵⏵" not in l:
                last_line = l[:80]
                break

        click.echo(f"  {name}")
        click.echo(f"    tmux attach -t {name}")
        if last_line:
            click.echo(f"    {last_line}")
        click.echo()

    if any("moda-manager" in l for l in result.stdout.splitlines()):
        click.echo(f"  Manager: running (tmux attach -t moda-manager)")


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
def register(repo_path: str):
    """Register a repo with modabot."""
    config = GlobalConfig.load()
    path = Path(repo_path).resolve()

    if path in config.repos:
        click.echo(f"Already registered: {path}")
        return

    if not (path / ".modastack.yaml").exists():
        click.echo(f"Warning: No .modastack.yaml in {path}")

    config.repos.append(path)
    config.save()
    click.echo(f"Registered: {path}")


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


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--task-tracking", type=click.Choice(["github-issues", "linear"]), default=None,
              help="Task tracking system (default: github-issues)")
@click.option("--project", default=None, help="Project prefix (e.g., BET, TESS)")
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None, help="Linear API key (only for --task-tracking linear)")
@click.option("--non-interactive", is_flag=True, envvar="CI")
def setup(repo_path: str, task_tracking: str | None, project: str | None,
          linear_key: str | None, non_interactive: bool):
    """Set up a repo for modabot."""
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

    # Default to github-issues — no prompt needed
    if not task_tracking:
        if linear_key:
            task_tracking = "linear"
        else:
            task_tracking = "github-issues"

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

    # Auto-register
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
    # Engineer skills + product manager skills + shared tools
    skill_dirs = [
        repo_root / "engineer" / "process",
        repo_root / "engineer" / "practices",
        repo_root / "product_manager",
        repo_root / "tools",
    ]
    for category_dir in skill_dirs:
        if not category_dir.exists():
            continue
        for skill_dir in category_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                link = target_skills / skill_dir.name
                if link.exists() or link.is_symlink():
                    continue
                link.symlink_to(skill_dir.resolve())
                installed.append(skill_dir.name)
    if installed:
        for name in sorted(installed):
            click.echo(f"  Linked /{name}")
    else:
        click.echo("  Skills already installed.")


@main.command()
@click.option("--port", default=8095, help="Dashboard server port")
def dashboard(port):
    """Start the web dashboard."""
    from dashboard.app import run_dashboard
    run_dashboard(port=port)


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
