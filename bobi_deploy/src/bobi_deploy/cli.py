"""The deploy CLI commands, delivered into `bobi` as plugins.

Each command here is registered under the `bobi.commands` entry-point group
(see pyproject.toml); `bobi.cli` discovers and mounts them at startup. The
commands are plain top-level `click.command`s — they carry no runtime identity
(deploy is machine/repo scoped, like the rest of the top-level CLI).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from bobi.build import BuildError


@click.command()
@click.argument("name")
@click.option("--team", default=None,
              help="Local team package (agents/<team>) → ssh-push delivery.")
@click.option("--team-url", default=None,
              help="Published team .tar.gz URL → HTTPS-fetch delivery.")
@click.option("--fleet", default=None, help="Fleet namespace (app = <fleet>-<name>).")
@click.option("--env-file", "env_file", default=None,
              help="KEY=VALUE secrets file (overrides secrets.env-file).")
@click.option("--auth", default=None, type=click.Choice(["api_key", "subscription"]))
@click.option("--event-server", "event_server", default=None, help="Event server URL.")
@click.option("--region", default=None, help="Fly region.")
@click.option("--memory", default=None, help="Machine memory, e.g. 8gb.")
@click.option("--cpus", default=None, type=int, help="Shared vCPUs.")
@click.option("--volume-size", "volume_size", default=None, type=int, help="Volume GB.")
@click.option("--login-channel", "login_channel", default=None,
              help="Subscription mode: Slack channel for first-boot login (C23).")
@click.option("--claude-version", "claude_version", default=None,
              help="Pin the claude CLI version baked into the image.")
@click.option("--org", default=None, help="Fly org slug.")
@click.option("--rebuild", is_flag=True,
              help="In-place update: force an image rebuild instead of a hot-push "
                   "(also automatic when a team's build: deps change — #379).")
@click.option("--no-prune", "no_prune", is_flag=True,
              help="Don't remove live Fly secrets that aren't declared in the "
                   "team's agent.yaml. By default an update reconciles to the "
                   "declared set, pruning undeclared secrets (#385).")
def deploy(name, team, team_url, fleet, env_file, auth, event_server, region,
           memory, cpus, volume_size, login_channel, claude_version, org,
           rebuild, no_prune):
    """Provision or update ONE instance — the deployment primitive.

    NAME selects the deployment: deployments/<name>.yaml (merged over
    deployments/defaults.yaml and built-ins), or — with no file — the local
    package agents/<name> (ssh-push). Flags override the resolved config.

    Idempotent: no Fly app yet → provision; app exists → in-place update.
    Two delivery modes, picked by the team source:
      team:     <name>  local package  → ssh-push (build, push over fly ssh, start)
      team-url: <url>   published tarball → HTTPS-fetch at first boot

    Works from the binary alone — no checkout. In a bobi checkout the image
    builds from source; otherwise it builds from PyPI (the bundled deploy assets).
    Composes with orchestration on top — a GitHub Action / Terraform / a for-loop
    calls this per instance; the looping/diffing lives there, never here.

    Usage:
        bobi deploy eng-team            # uses deployments/eng-team.yaml
        bobi deploy my-team --team my-team   # local package, ssh-push
    """
    from bobi_deploy import deploy as deploy_mod
    project_path = Path.cwd()

    overrides = {
        "team": team, "team_url": team_url, "fleet": fleet, "auth": auth,
        "event_server": event_server, "region": region, "memory": memory,
        "cpus": cpus, "volume_size": volume_size, "login_channel": login_channel,
        "claude_version": claude_version, "org": org,
        "rebuild": rebuild, "no_prune": no_prune,
    }
    if env_file:
        overrides["secrets_env_file"] = env_file

    try:
        cfg = deploy_mod.load_deploy_config(project_path, name, overrides)
    except (deploy_mod.DeployError, BuildError) as e:
        raise click.UsageError(str(e))

    # Preflight: guide the user (or an agent) through Fly setup before we build.
    deploy_mod.preflight_fly_or_exit()

    click.echo(
        f"Deploying '{name}' → app {cfg.app_name} "
        f"(fleet {cfg.fleet_stamp}, {cfg.delivery} delivery)"
    )
    try:
        deploy_mod.deploy(project_path, name, overrides)
    except (deploy_mod.DeployError, BuildError) as e:
        click.echo(f"Deploy failed: {e}", err=True)
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        click.echo(f"Deploy failed (exit {e.returncode}).", err=True)
        raise SystemExit(1)
    click.echo(f"Deployed '{name}' (app {cfg.app_name}).")


@click.command("deploy-init")
@click.argument("team", required=False)
@click.option("--fleet", default="myfleet",
              help="Fleet namespace (app names become <fleet>-<team>).")
@click.option("--tenant", default="prod",
              help="Default GitHub Environment that holds the secrets.")
@click.option("--event-server", "event_server", default=None,
              help="Event server URL (omit for the shared moda Worker).")
@click.option("--auth", default="api_key",
              type=click.Choice(["api_key", "subscription"]),
              help="Auth mode written into the deployment(s) and reflected in the "
                   "printed secret list (subscription drops ANTHROPIC_API_KEY).")
@click.option("--force", is_flag=True, help="Overwrite existing scaffold files.")
def deploy_init(team, fleet, tenant, event_server, auth, force):
    """Scaffold CI deploy automation for this repo's agent teams (bring-your-own-repo).

    Generates a standalone .github/workflows/deploy-agent-teams.yml (installs
    bobi from PyPI — no framework checkout needed) + a deployments/ skeleton,
    then prints the exact `fly`/`gh` commands to wire the Fly token and per-key
    secrets, derived from each team's declared ${VAR} set so the list is correct.
    Non-destructive: existing files are skipped unless --force.

    TEAM scopes to one team under agents/; omit to scaffold every team found.

    Usage:
        bobi deploy-init                          # every team under agents/
        bobi deploy-init eng-team --fleet acme --tenant prod
    """
    from bobi.build import _bobi_version
    from bobi_deploy import scaffold as scaffold_mod

    project_path = Path.cwd()
    if team and not (project_path / "agents" / team / "agent.yaml").is_file():
        raise click.UsageError(f"no agents/{team}/agent.yaml found.")
    teams = [team] if team else scaffold_mod.discover_teams(project_path)
    if not teams:
        raise click.UsageError(
            "no teams found under agents/ (expected agents/<team>/agent.yaml). "
            "Pass a TEAM name or run from your agent-teams repo root.")

    try:
        version = _bobi_version()
    except BuildError:
        version = "<version>"  # bobi not pip-installed; user pins manually

    result = scaffold_mod.scaffold(
        project_path, teams=teams, fleet=fleet, tenant=tenant,
        event_server=event_server, auth=auth, force=force, version=version)

    for p in result.written:
        click.echo(f"  wrote   {p.relative_to(project_path)}")
    for p in result.skipped:
        click.echo(f"  skipped {p.relative_to(project_path)} "
                   "(exists; --force to overwrite)")
    if not result.written:
        click.echo("Nothing written (all files already exist).")
    click.echo("")
    click.echo(scaffold_mod.next_steps(result))


@click.command()
@click.argument("name")
@click.option("--fleet", default=None, help="Fleet namespace (app = <fleet>-<name>).")
@click.option("--yes", is_flag=True, help="Skip the typed-confirmation (automation).")
def destroy(name, fleet, yes):
    """Tear down ONE instance — its Fly app AND its volume.

    Resolves NAME → app <fleet>-<name> (from deployments/<name>.yaml or --fleet)
    and destroys it. The volume is the only copy of the instance's state, so this
    keeps a typed-confirmation; --yes is for automation.

    Usage:
        bobi destroy eng-team
        bobi destroy eng-team --yes
    """
    from bobi_deploy import deploy as deploy_mod
    deploy_mod.preflight_fly_or_exit()

    overrides = {"fleet": fleet} if fleet else None
    try:
        app = deploy_mod.destroy(Path.cwd(), name, overrides, assume_yes=yes)
    except (deploy_mod.DeployError, BuildError) as e:
        raise click.UsageError(str(e))
    except subprocess.CalledProcessError as e:
        click.echo(f"Destroy failed (exit {e.returncode}).", err=True)
        raise SystemExit(1)
    click.echo(f"Destroyed '{name}' (app {app}).")
