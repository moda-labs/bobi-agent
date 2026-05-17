"""CLI interface for the dispatch engine."""

import json
import logging
import sys
from pathlib import Path

import click

from .config import GlobalConfig, GLOBAL_CONFIG_DIR
from .engine import run
from .setup import setup_repo, generate_dispatch_yaml
from .state import StateStore


@click.group()
def main():
    """Agent dispatch engine — scan Linear/Slack, spawn coding agents."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
def cycle():
    """Run one dispatch cycle (the cron entrypoint)."""
    summary = run()
    click.echo(json.dumps(summary, indent=2))


@main.command()
def status():
    """Show current in-flight work."""
    state = StateStore()
    items = state.get_in_flight()

    if not items:
        click.echo("No in-flight work.")
        return

    for item in items:
        click.echo(
            f"  [{item.status.value:>10}] {item.id}: {item.title}"
            f" (repo: {Path(item.repo_path).name})"
        )


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
def register(repo_path: str):
    """Register a repo with the dispatch engine."""
    config = GlobalConfig.load()
    path = Path(repo_path).resolve()

    if path in config.repos:
        click.echo(f"Already registered: {path}")
        return

    # Check for .dispatch.yaml
    if not (path / ".dispatch.yaml").exists():
        click.echo(f"Warning: No .dispatch.yaml in {path}")
        click.echo("Create one to configure how agents work on this repo.")

    config.repos.append(path)
    config.save()
    click.echo(f"Registered: {path}")


@main.command()
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None, help="Linear API key")
@click.option("--slack-token", envvar="SLACK_BOT_TOKEN", default=None, help="Slack bot token")
@click.option("--non-interactive", is_flag=True, envvar="CI", help="Skip prompts (use flags/env vars only)")
def init(linear_key, slack_token, non_interactive):
    """Initialize global config at ~/.dispatch/.

    In non-interactive mode (--non-interactive, or CI=1), skips prompts and
    only uses values from flags or env vars. Safe to run from agents/scripts.
    """
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()

    if linear_key:
        config.linear_api_key = linear_key
    elif not config.linear_api_key and not non_interactive:
        try:
            key = click.prompt("Linear API key (or press Enter to skip)", default="", show_default=False)
            if key:
                config.linear_api_key = key
        except (EOFError, click.Abort):
            pass

    if slack_token:
        config.slack_bot_token = slack_token
    elif not config.slack_bot_token and not non_interactive:
        try:
            token = click.prompt("Slack bot token (or press Enter to skip)", default="", show_default=False)
            if token:
                config.slack_bot_token = token
        except (EOFError, click.Abort):
            pass

    config.save()
    click.echo(f"Config saved to {GLOBAL_CONFIG_DIR}/config.yaml")


@main.command()
def repos():
    """List registered repos and their dispatch status."""
    config = GlobalConfig.load()

    if not config.repos:
        click.echo("No repos registered. Use `dispatch register <path>` to add one.")
        return

    for path in config.repos:
        has_config = (path / ".dispatch.yaml").exists()
        status = "ready" if has_config else "no .dispatch.yaml"
        click.echo(f"  {path.name:30s} [{status}] {path}")


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--linear-project", default=None, help="Linear project key (e.g., ENG)")
@click.option("--slack-channel", default=None, help="Slack channel for notifications (e.g., #eng-agents)")
@click.option("--non-interactive", is_flag=True, envvar="CI", help="Skip prompts, use flags/defaults only")
def setup(repo_path: str, linear_project: str | None, slack_channel: str | None, non_interactive: bool):
    """Auto-generate .dispatch.yaml for a repo and register it.

    Inspects the repo to detect test commands, framework, and suggests
    Linear project key and gstack skills. One command to wire up any repo.
    """
    path = Path(repo_path).resolve()
    config_path = path / ".dispatch.yaml"

    if config_path.exists() and not non_interactive:
        try:
            if not click.confirm(f".dispatch.yaml already exists in {path}. Overwrite?"):
                click.echo("Aborted.")
                return
        except (EOFError, click.Abort):
            click.echo("Overwriting (non-interactive).")

    # Generate config
    from .setup import generate_dispatch_yaml
    import yaml

    config = generate_dispatch_yaml(path)

    # Override with flags if provided
    if linear_project:
        config["linear"]["project"] = linear_project
    if slack_channel:
        config["notify"]["slack_channel"] = slack_channel

    # Write it
    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    click.echo(f"Generated: {config_path}")

    # Show what was detected vs what needs user input
    test_cmd = config.get("verify", {}).get("test_command", "")
    project = config.get("linear", {}).get("project", "")
    skills = config.get("agent", {}).get("skills", [])
    channel = config.get("notify", {}).get("slack_channel", "")

    click.echo("")
    click.echo("Detected:")
    click.echo(f"  Test command:   {test_cmd or '(none detected)'}")
    click.echo(f"  Skills:         {', '.join(skills)}")
    click.echo("")
    click.echo("Needs user input:")
    if not linear_project:
        click.echo(f"  Linear project: \"{project}\" (GUESSED from repo name — please verify)")
    else:
        click.echo(f"  Linear project: {project} (set via flag)")
    if not channel:
        click.echo(f"  Slack channel:  (not set — add to .dispatch.yaml)")
    else:
        click.echo(f"  Slack channel:  {channel}")
    click.echo("")

    # Auto-register
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo("Registered with dispatch engine.")
    else:
        click.echo("Already registered.")

    # Clear guidance for the agent/user
    click.echo("")
    if not linear_project or not channel:
        click.echo("ACTION REQUIRED:")
        if not linear_project:
            click.echo("  1. Set linear.project in .dispatch.yaml to your Linear project key")
            click.echo("     (the prefix on issue IDs, e.g., ENG if issues are ENG-42)")
        if not channel:
            click.echo("  2. Set notify.slack_channel in .dispatch.yaml (e.g., #eng-agents)")
        click.echo("")
        click.echo("Or re-run with flags:")
        click.echo(f"  dispatch setup {repo_path} --linear-project YOUR_KEY --slack-channel '#your-channel'")


if __name__ == "__main__":
    main()
