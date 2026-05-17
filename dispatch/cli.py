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
def setup(repo_path: str):
    """Auto-generate .dispatch.yaml for a repo and register it.

    Inspects the repo to detect test commands, framework, and suggests
    Linear project key and gstack skills. One command to wire up any repo.
    """
    path = Path(repo_path).resolve()
    config_path = path / ".dispatch.yaml"

    if config_path.exists():
        if not click.confirm(f".dispatch.yaml already exists in {path}. Overwrite?"):
            click.echo("Aborted.")
            return

    # Generate and write
    output = setup_repo(path)
    click.echo(f"Generated: {output}")

    # Show what was detected
    import yaml
    content = yaml.safe_load(config_path.read_text())
    test_cmd = content.get("verify", {}).get("test_command", "")
    project = content.get("linear", {}).get("project", "")
    skills = content.get("agent", {}).get("skills", [])

    click.echo(f"  Linear project: {project}")
    click.echo(f"  Test command:   {test_cmd or '(none detected)'}")
    click.echo(f"  Skills:         {', '.join(skills)}")
    click.echo("")

    # Auto-register
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo(f"Registered with dispatch engine.")
    else:
        click.echo(f"Already registered.")

    click.echo("")
    click.echo("Next: edit .dispatch.yaml to set your Linear project key and Slack channel.")


if __name__ == "__main__":
    main()
