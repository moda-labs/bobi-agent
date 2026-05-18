"""CLI interface for the dispatch engine."""

import json
import logging
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from .config import GlobalConfig, GLOBAL_CONFIG_DIR
from .daemon import run
from .setup import generate_dispatch_yaml
from .state import StateStore

LOG_PATH = GLOBAL_CONFIG_DIR / "dispatch.log"


@click.group()
def main():
    """Agent dispatch engine — scan Linear, spawn coding agents."""
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
@click.option("--interval", default=5, help="Seconds between cycles")
def daemon(interval):
    """Run dispatch as a long-running daemon. Best run in tmux.

    This is the most reliable way to run dispatch — it inherits your
    full shell environment including macOS Keychain access for Claude
    OAuth. No cron or launchd needed.

    Usage:
        dispatch daemon              # foreground, 5s interval
        tmux new -d -s dispatch 'dispatch daemon'  # background in tmux
    """
    import time as time_mod
    log = logging.getLogger(__name__)
    log.info(f"Daemon starting. Polling every {interval}s.")
    click.echo(f"Dispatch daemon starting. Polling every {interval}s. Ctrl+C to stop.")
    click.echo(f"Logs: {LOG_PATH}")
    try:
        while True:
            try:
                summary = run()
                if any(v > 0 for k, v in summary.items() if k != "skipped"):
                    log.info(f"Cycle: {json.dumps(summary)}")
            except Exception as e:
                log.error(f"Cycle failed: {e}")
            time_mod.sleep(interval)
    except KeyboardInterrupt:
        log.info("Daemon stopped.")
        click.echo("\nDaemon stopped.")


@main.command()
def cycle():
    """Run one dispatch cycle (manual/debugging)."""
    summary = run()
    click.echo(json.dumps(summary, indent=2))


@main.command()
def status():
    """Show current in-flight work."""
    import time as time_mod

    from .session import session_exists, detect_state

    state = StateStore()
    agents = state.all_agents()

    if not agents:
        click.echo("No tracked work.")
        return

    for agent in agents:
        elapsed = time_mod.time() - agent.started_at
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        alive = session_exists(agent.issue_id)
        sess = detect_state(agent.issue_id) if alive else {"state": "exited"}
        sess_label = sess["state"]

        stall_elapsed = time_mod.time() - agent.last_activity_at
        stall_mins = int(stall_elapsed // 60)

        click.echo(f"  {agent.issue_id:10s} {agent.title}")
        click.echo(f"             {sess_label}, {mins}m{secs}s, phase={agent.last_phase or 'starting'}")
        if alive:
            click.echo(f"             tmux attach -t agentd-{agent.issue_id.lower()}")
        if stall_mins > 0 and alive:
            click.echo(f"             last activity: {stall_mins}m ago")
        if sess.get("question"):
            click.echo(f"             Q: {sess['question'][:80]}")
        click.echo()


@main.command()
@click.option("--interval", default=5, help="Refresh interval in seconds")
def watch(interval: int):
    """Live dashboard — refreshes every N seconds. Ctrl+C to stop."""
    import os
    import time as time_mod

    from .session import session_exists, detect_state

    try:
        while True:
            os.system("clear")
            store = StateStore()
            agents = store.all_agents()
            now = time_mod.time()

            click.echo("agentd | Ctrl+C to stop")
            click.echo(f"{'─' * 70}")

            if not agents:
                click.echo("\n  No tracked work.\n")
            else:
                for agent in agents:
                    elapsed = now - agent.started_at
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)

                    alive = session_exists(agent.issue_id)
                    sess = detect_state(agent.issue_id) if alive else {"state": "exited"}
                    icon = "●" if alive else "○"
                    stall = int((now - agent.last_activity_at) // 60)

                    click.echo(f"\n  {icon} {agent.issue_id:10s} {agent.title}")
                    click.echo(f"    {sess['state']} | {mins}m{secs}s | phase={agent.last_phase or 'starting'}")
                    if alive:
                        click.echo(f"    tmux attach -t agentd-{agent.issue_id.lower()}")
                    if stall > 0 and alive:
                        click.echo(f"    last activity {stall}m ago")
                    if sess.get("question"):
                        click.echo(f"    Q: {sess['question'][:60]}")
                    if sess.get("options"):
                        for opt in sess["options"][:4]:
                            click.echo(f"       {opt}")

            click.echo(f"\n{'─' * 70}")
            click.echo(f"  Refreshing every {interval}s...")
            time_mod.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


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
    """Initialize global config and start the dispatch daemon.

    Creates the config directory and empty config. Starts the daemon
    in a tmux session so it runs in the background with full env
    (including macOS Keychain access for Claude OAuth).
    Credentials are stored per-project — use `dispatch setup` in each repo.
    """
    import subprocess, shutil

    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")

    # Start daemon in tmux
    tmux_path = shutil.which("tmux")
    dispatch_bin = Path(sys.executable).parent / "dispatch"

    if not tmux_path:
        click.echo("tmux not found. Run the daemon manually: dispatch daemon")
        return

    # Check if already running
    result = subprocess.run(
        [tmux_path, "has-session", "-t", "dispatch"],
        capture_output=True,
    )
    if result.returncode == 0:
        click.echo("Daemon already running in tmux session 'dispatch'.")
        return

    # Start new tmux session with the daemon
    subprocess.run(
        [tmux_path, "new-session", "-d", "-s", "dispatch", f"{dispatch_bin} daemon"],
        check=True,
    )
    click.echo("Daemon started in tmux session 'dispatch'.")
    click.echo("  Attach: tmux attach -t dispatch")
    click.echo("  Logs:   dispatch watch")


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

    # Bootstrap Linear board with required workflow states
    resolved_key = linear_key or (creds.get(credential_name) or {}).get("linear_api_key")
    resolved_project = linear_project or config["linear"]["project"]
    if resolved_key and resolved_project:
        click.echo("")
        click.echo("Bootstrapping Linear board...")
        from .board_setup import bootstrap_board
        import truststore
        truststore.inject_into_ssl()
        actions = bootstrap_board(resolved_key, resolved_project)
        for action in actions:
            click.echo(f"  {action}")

    # Install dispatch skills into the repo's .claude/skills/
    click.echo("")
    click.echo("Installing dispatch skills...")
    agentd_skills = Path(__file__).parent.parent / "skills"
    target_skills = path / ".claude" / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    installed = []
    for skill_dir in agentd_skills.iterdir():
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
