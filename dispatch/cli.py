"""CLI interface for modabot."""

import json
import logging
import sys
from importlib.metadata import version
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from .config import GlobalConfig, GLOBAL_CONFIG_DIR
from .setup import generate_dispatch_yaml
from .state import StateStore

LOG_PATH = GLOBAL_CONFIG_DIR / "dispatch.log"


@click.group()
@click.version_option(version=version("agentd"), prog_name="dispatch")
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
        dispatch start                    # polling mode (default)
        dispatch start --webhooks         # webhook + polling mode
        dispatch start --webhooks --port 9090
    """
    from manager.events.consumer import run
    run(webhook_port=port, use_webhooks=webhooks, batch_window=batch_window)


@main.command()
def tick():
    """Run one manager tick (for debugging)."""
    from manager.loop import run_once
    result = run_once()
    click.echo(json.dumps(result, indent=2))


@main.command()
def status():
    """Show active engineer sessions."""
    from .session import session_exists, detect_state

    state = StateStore()
    agents = state.all_agents()

    if not agents:
        click.echo("No active engineers.")
        return

    import time as time_mod
    for agent in agents:
        elapsed = time_mod.time() - agent.started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        alive = session_exists(agent.issue_id)
        sess = detect_state(agent.issue_id) if alive else {"state": "exited"}

        stall = int((time_mod.time() - agent.last_activity_at) / 60)

        click.echo(f"  {agent.issue_id:10s} {agent.title}")
        click.echo(f"             {sess['state']}, {mins}m{secs}s, phase={agent.last_phase or 'starting'}")
        if alive:
            click.echo(f"             tmux attach -t agentd-{agent.issue_id.lower()}")
        if stall > 0 and alive:
            click.echo(f"             last activity: {stall}m ago")
        if sess.get("question"):
            click.echo(f"             Q: {sess['question'][:80]}")
        click.echo()


@main.command()
def decisions():
    """Show recent manager decisions."""
    decisions_path = Path.home() / ".dispatch" / "manager" / "decisions.jsonl"
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

    if not (path / ".dispatch.yaml").exists():
        click.echo(f"Warning: No .dispatch.yaml in {path}")

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
    click.echo("Run `dispatch setup <repo>` to add a repo.")


@main.command()
def repos():
    """List registered repos."""
    config = GlobalConfig.load()
    if not config.repos:
        click.echo("No repos registered.")
        return
    for path in config.repos:
        has_config = (path / ".dispatch.yaml").exists()
        click.echo(f"  {path.name:30s} [{'ready' if has_config else 'no config'}] {path}")


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--linear-project", default=None)
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None)
@click.option("--non-interactive", is_flag=True, envvar="CI")
def setup(repo_path: str, linear_project: str | None, linear_key: str | None, non_interactive: bool):
    """Set up a repo for modabot."""
    import yaml

    path = Path(repo_path).resolve()
    config_path = path / ".dispatch.yaml"
    credential_name = path.name

    if config_path.exists() and not non_interactive:
        try:
            if not click.confirm(f".dispatch.yaml exists in {path}. Overwrite?"):
                return
        except (EOFError, click.Abort):
            pass

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

    config = generate_dispatch_yaml(path)
    config["credentials"] = credential_name
    if linear_project:
        config["linear"]["project"] = linear_project

    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    click.echo(f"Generated: {config_path}")

    # Auto-register
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo("Registered.")

    # Bootstrap Linear board
    resolved_key = linear_key or (creds.get(credential_name) or {}).get("linear_api_key")
    resolved_project = linear_project or config["linear"]["project"]
    if resolved_key and resolved_project:
        click.echo("Bootstrapping Linear board...")
        from .board_setup import bootstrap_board
        for action in bootstrap_board(resolved_key, resolved_project):
            click.echo(f"  {action}")

    # Install skills
    click.echo("Installing skills...")
    skills_root = Path(__file__).parent.parent / "engineer"
    target_skills = path / ".claude" / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    installed = []
    for category in ["process", "practices", "tools"]:
        category_dir = skills_root / category
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


if __name__ == "__main__":
    main()
