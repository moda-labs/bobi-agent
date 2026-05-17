"""CLI interface for the dispatch engine."""

import json
import logging
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from .config import GlobalConfig, GLOBAL_CONFIG_DIR
from .engine import run
from .setup import setup_repo, generate_dispatch_yaml
from .state import StateStore


CRON_COMMENT = "# agent-dispatch: scan Linear and dispatch work"
CRON_JOB = "* * * * * {dispatch} cycle >> {log} 2>&1"


def _get_cron_line() -> str:
    """Build the cron line using the venv's dispatch binary."""
    dispatch_bin = Path(sys.executable).parent / "dispatch"
    log_path = GLOBAL_CONFIG_DIR / "dispatch.log"
    return CRON_JOB.format(dispatch=dispatch_bin, log=log_path)


@click.group()
def main():
    """Agent dispatch engine — scan Linear, spawn coding agents."""
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
@click.option("--non-interactive", is_flag=True, envvar="CI", help="Skip prompts (use flags/env vars only)")
def init(non_interactive):
    """Initialize global config and install the cron job.

    Creates the config directory, empty config, and installs the
    cron job (if not already running). Credentials are stored
    per-project — use `dispatch setup` in each repo.
    """
    import subprocess

    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")

    # Auto-install cron if not already running
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    current = result.stdout if result.returncode == 0 else ""

    if CRON_COMMENT not in current:
        cron_line = _get_cron_line()
        new_crontab = current.rstrip("\n") + f"\n{CRON_COMMENT}\n{cron_line}\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
        click.echo("Cron installed. Dispatch runs every minute.")
    else:
        click.echo("Cron already running.")


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
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None, help="Linear API key for this project")
@click.option("--non-interactive", is_flag=True, envvar="CI", help="Skip prompts, use flags/defaults only")
def setup(repo_path: str, linear_project: str | None, linear_key: str | None, non_interactive: bool):
    """Auto-generate .dispatch.yaml for a repo and register it.

    Inspects the repo to detect test commands, framework, and suggests
    Linear project key and gstack skills. Stores the Linear API key as
    a per-project credential (not global).
    """
    path = Path(repo_path).resolve()
    config_path = path / ".dispatch.yaml"
    credential_name = path.name  # use repo dir name as credential key

    if config_path.exists() and not non_interactive:
        try:
            if not click.confirm(f".dispatch.yaml already exists in {path}. Overwrite?"):
                click.echo("Aborted.")
                return
        except (EOFError, click.Abort):
            click.echo("Overwriting (non-interactive).")

    # Store Linear API key as a per-project credential
    from .config import Credentials
    creds = Credentials.load()
    existing_cred = creds.get(credential_name)
    has_key = bool(existing_cred.get("linear_api_key"))

    if linear_key:
        creds.add(credential_name, linear_api_key=linear_key)
        click.echo(f"Linear API key stored for project '{credential_name}'")
    elif not has_key and not non_interactive:
        try:
            key = click.prompt(
                "Linear API key (https://linear.app/settings/api → Create key)",
                default="", show_default=False,
            )
            if key:
                creds.add(credential_name, linear_api_key=key)
                click.echo(f"Linear API key stored for project '{credential_name}'")
        except (EOFError, click.Abort):
            pass
    elif has_key:
        click.echo(f"Linear API key already configured for '{credential_name}'")

    # Generate config
    from .setup import generate_dispatch_yaml
    import yaml

    config = generate_dispatch_yaml(path)
    config["credentials"] = credential_name

    # Override with flags if provided
    if linear_project:
        config["linear"]["project"] = linear_project

    # Write it
    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    click.echo(f"Generated: {config_path}")

    # Show what was detected vs what needs user input
    test_cmd = config.get("verify", {}).get("test_command", "")
    project = config.get("linear", {}).get("project", "")
    skills = config.get("agent", {}).get("skills", [])

    click.echo("")
    click.echo("Detected:")
    click.echo(f"  Test command:   {test_cmd or '(none detected)'}")
    click.echo(f"  Skills:         {', '.join(skills)}")
    click.echo(f"  Credentials:    {credential_name}")
    click.echo("")
    if not linear_project:
        click.echo(f"  Linear project: \"{project}\" (GUESSED from repo name — please verify)")
    else:
        click.echo(f"  Linear project: {project}")

    # Auto-register
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo("")
        click.echo("Registered with dispatch engine.")
    else:
        click.echo("")
        click.echo("Already registered.")

    # Guidance if project was guessed
    if not linear_project:
        click.echo("")
        click.echo("ACTION REQUIRED:")
        click.echo("  Set linear.project in .dispatch.yaml to your Linear project key")
        click.echo("  (the prefix on issue IDs, e.g., ENG if issues are ENG-42)")
        click.echo("")
        click.echo("Or re-run with:")
        click.echo(f"  dispatch setup {repo_path} --linear-project YOUR_KEY")


@main.command(name="cron")
@click.argument("action", type=click.Choice(["install", "uninstall", "status"]))
def cron_cmd(action: str):
    """Manage the dispatch cron job (install/uninstall/status)."""
    import subprocess

    # Read current crontab
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    current = result.stdout if result.returncode == 0 else ""

    cron_line = _get_cron_line()

    if action == "status":
        if CRON_COMMENT in current:
            click.echo("Cron is installed and running every minute.")
            click.echo(f"  {cron_line}")
        else:
            click.echo("Cron is not installed.")
        return

    if action == "install":
        if CRON_COMMENT in current:
            click.echo("Cron already installed.")
            return

        new_crontab = current.rstrip("\n") + f"\n{CRON_COMMENT}\n{cron_line}\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
        click.echo("Cron installed. Dispatch runs every minute.")
        click.echo(f"  {cron_line}")
        return

    if action == "uninstall":
        if CRON_COMMENT not in current:
            click.echo("Cron not installed, nothing to remove.")
            return

        lines = current.splitlines()
        new_lines = [l for l in lines if CRON_COMMENT not in l and "dispatch" not in l.split("#")[0]]
        new_crontab = "\n".join(new_lines) + "\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
        click.echo("Cron uninstalled.")


if __name__ == "__main__":
    main()
