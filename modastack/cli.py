"""CLI interface for modabot."""

import json
import logging
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from .config import GlobalConfig, RepoEntry, GLOBAL_CONFIG_DIR
from .setup import detect_linear_project, full_setup, install_skill_symlinks

LOG_PATH = GLOBAL_CONFIG_DIR / "modastack.log"

REMOTE_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


@click.group()
@click.version_option(version=version("modastack"), prog_name="modastack")
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
        if any("moda-manager" in l for l in result.stdout.splitlines()):
            click.echo("  Manager: running (tmux attach -t moda-manager)")
        return

    for name in sessions:
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
@click.argument("target")
@click.option("--linear-project", default="", help="Linear project key (e.g. BT)")
@click.option("--credentials", "credential_name", default="default", help="Credential set name")
def register(target: str, linear_project: str, credential_name: str):
    """Register a repo with modabot. TARGET is a local path or org/repo."""
    config = GlobalConfig.load()

    if REMOTE_PATTERN.match(target):
        repo_name = target.split("/")[-1]
        clone_path = GLOBAL_CONFIG_DIR / "repos" / repo_name

        if clone_path.exists():
            click.echo(f"Already cloned: {clone_path}")
        else:
            click.echo(f"Cloning {target}...")
            clone_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["gh", "repo", "clone", target, str(clone_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                click.echo(f"Clone failed: {result.stderr.strip()}")
                sys.exit(1)

        path = clone_path
        remote = target
    else:
        path = Path(target).resolve()
        if not path.exists():
            click.echo(f"Path not found: {path}")
            sys.exit(1)
        remote = ""

    existing = config.get_repo(path)
    if existing:
        click.echo(f"Already registered: {path}")
        return

    if not linear_project:
        linear_project = detect_linear_project(path)

    installed = install_skill_symlinks(path)
    if installed:
        click.echo("Installed skills:")
        for name in installed:
            click.echo(f"  /{name}")

    entry = RepoEntry(
        path=path,
        remote=remote,
        linear_project=linear_project,
        credentials=credential_name,
    )
    config.repos.append(entry)
    config.save()
    click.echo(f"Registered: {path} (project: {linear_project})")


@main.command()
@click.option("--non-interactive", is_flag=True, envvar="CI")
@click.option("--git-name", default="Modabot")
@click.option("--git-email", default=None)
def init(non_interactive, git_name, git_email):
    """Initialize global config."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")

    current_name = subprocess.run(
        ["git", "config", "--global", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()

    if not current_name or non_interactive:
        subprocess.run(["git", "config", "--global", "user.name", git_name])
        email = git_email or "modabot@modastack.dev"
        subprocess.run(["git", "config", "--global", "user.email", email])
        click.echo(f"Git identity: {git_name} <{email}>")
    else:
        click.echo(f"Git identity already set: {current_name}")

    click.echo("Run `modastack register <repo>` to add a repo.")


@main.command()
def repos():
    """List registered repos."""
    config = GlobalConfig.load()
    if not config.repos:
        click.echo("No repos registered.")
        return
    for entry in config.repos:
        status = entry.linear_project or "no project"
        remote_info = f" ({entry.remote})" if entry.remote else ""
        click.echo(f"  {entry.path.name:30s} [{status}]{remote_info} {entry.path}")


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--linear-project", default=None)
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None)
@click.option("--non-interactive", is_flag=True, envvar="CI")
def setup(repo_path: str, linear_project: str | None, linear_key: str | None, non_interactive: bool):
    """Set up a repo for modabot — install skills, store credentials, register."""
    path = Path(repo_path).resolve()
    credential_name = path.name

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

    if not linear_project:
        linear_project = detect_linear_project(path)

    # Install skills
    click.echo("Installing skills...")
    installed = install_skill_symlinks(path)
    if installed:
        for name in installed:
            click.echo(f"  Linked /{name}")
    else:
        click.echo("  Skills already installed.")

    # Register
    global_config = GlobalConfig.load()
    existing = global_config.get_repo(path)
    if not existing:
        entry = RepoEntry(
            path=path,
            linear_project=linear_project or "",
            credentials=credential_name,
        )
        global_config.repos.append(entry)
        global_config.save()
        click.echo("Registered.")
    else:
        click.echo("Already registered.")

    # Bootstrap Linear board
    resolved_key = linear_key or (creds.get(credential_name) or {}).get("linear_api_key")
    if resolved_key and linear_project:
        click.echo("Bootstrapping Linear board...")
        from .board_setup import bootstrap_board
        for action in bootstrap_board(resolved_key, linear_project):
            click.echo(f"  {action}")


if __name__ == "__main__":
    main()
