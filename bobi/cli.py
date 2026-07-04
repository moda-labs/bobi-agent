"""CLI interface for bobi."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import click

from bobi import paths
from bobi.install import (
    install_pack as _install_pack,
    write_install_gitignore as _write_install_gitignore,
)

from .__version__ import __version__

_PACKAGE_DIR = Path(__file__).parent

# Prompt hints for framework-level env vars an agent.yaml may reference.
# These are not credentials in the secret sense, so tell the user what a
# blank answer means instead of implying a value is required.
_ENV_VAR_HINTS = {
    "BOBI_EVENT_SERVER":
        "event server URL - leave blank to auto-start the local server",
}


def _interactive_terminal() -> bool:
    """True when both ends of the session are a real terminal.

    Split out so tests can stub interactivity (the test runner replaces
    stdin/stdout with pipes).
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_startup_info(project_path: Path, pid: int, log_file: Path):
    """Print a startup summary with environment info."""
    from bobi.service import build_startup_info

    info = build_startup_info(project_path, pid, log_file)

    W = 16  # column width for labels
    lines = []
    lines.append(f"bobi v{info.version}")
    lines.append(f"  {'slot':<{W}}{info.agent_name} ({info.project_path})")
    lines.append(f"  {'pid':<{W}}{info.pid}")
    if info.package:
        lines.append(f"  {'package':<{W}}{info.package}")
    lines.append(
        f"  {'event server':<{W}}{info.event_server_url} ({info.event_server_label})"
    )
    if info.workflows:
        lines.append(f"  {'workflows':<{W}}{', '.join(info.workflows)}")
    if info.monitors:
        lines.append(f"  {'monitors':<{W}}{', '.join(info.monitors)}")
    lines.append(f"  {'logs':<{W}}{info.log_file}")

    click.echo("\n".join(lines))


def _detect_project_root(cwd: Path | None = None) -> Path:
    """Resolve and bind an already-selected runtime root.

    This only honors inherited ``BOBI_ROOT`` or an explicit runtime root. It
    requires an explicit runtime root; interactive runtime commands should be
    invoked through the named-agent CLI so the agent group can bind identity once.
    """
    bound = paths.bound_root()
    if bound is not None:
        return bound
    try:
        root = paths.resolve_root(cwd)
    except RuntimeError as e:
        raise click.UsageError(str(e))
    paths.bind_root(root)
    return root


def _project_state_dir(project_path: Path) -> Path:
    """Runtime state directory for a project's manager."""
    return paths.state_dir(project_path)


def _parse_local_event_server_port(url: str) -> int | None:
    """Return the local event-server port from a URL, or None for remote URLs."""
    if not url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _event_server_port_file(project_path: Path) -> Path:
    return _project_state_dir(project_path) / "event-server.port"


def _selected_local_event_server_port(
    project_path: Path,
    override: int | None = None,
) -> int:
    """Port for the selected runtime's local event server.

    Explicit CLI overrides win, then a live runtime's remembered start port,
    then the configured local event_server_url, then the default 8080.
    """
    if override is not None:
        return override

    pid_file = _project_state_dir(project_path) / "event-server.pid"
    port_file = _event_server_port_file(project_path)
    if pid_file.exists() and port_file.exists():
        try:
            return int(port_file.read_text().strip())
        except (OSError, ValueError):
            pass

    try:
        from .config import Config
        configured = Config.load(project_path).event_server_url
    except Exception:
        configured = ""
    if configured:
        port = _parse_local_event_server_port(configured)
        if port is not None:
            return port

    if port_file.exists():
        try:
            return int(port_file.read_text().strip())
        except (OSError, ValueError):
            pass
    return 8080


def _ensure_root_bound() -> Path:
    """Bind the installation root if no entry point has yet — the call is
    for its side effect. Raises a clean UsageError outside an install."""
    root = paths.bound_root()
    return root if root is not None else _detect_project_root()


def _try_detect_project_root() -> Path | None:
    """Best-effort runtime binding from inherited BOBI_ROOT only."""
    try:
        return _detect_project_root()
    except click.UsageError:
        return None


def _bind_agent_runtime(name: str) -> Path:
    try:
        root = paths.resolve_root_for_agent(name)
    except RuntimeError as e:
        raise click.UsageError(str(e))
    paths.bind_root(root)
    _attach_runtime_log(root)
    return root


def _attach_runtime_log(root: Path) -> None:
    state = _project_state_dir(root)
    log_path = state / "manager.log"
    logger = logging.getLogger()
    if not any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(log_path)
        for h in logger.handlers
    ):
        logger.addHandler(logging.FileHandler(log_path))




@click.group()
@click.version_option(version=__version__, prog_name="bobi")
def main():
    """Bobi — build teams of event-driven AI agents."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler()],
    )
    # Top-level commands are machine/repo scoped. Runtime identity is bound by
    # `bobi agent <name> ...` or inherited BOBI_ROOT in child processes.
    return


@main.group()
@click.argument("name")
@click.pass_context
def agent(ctx, name):
    """Operate on one installed Bobi Agent runtime."""
    if ctx.invoked_subcommand == "ui":
        ctx.obj = {"agent": name, "root": None}
        return
    root = _bind_agent_runtime(name)
    ctx.obj = {"agent": name, "root": root}


def _has_systemd_service() -> bool:
    """Check if bobi is managed by a systemd user service."""
    svc = Path.home() / ".config" / "systemd" / "user" / "bobi.service"
    if not svc.exists():
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "bobi"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _systemctl(action: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", action, "bobi"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        click.echo(f"systemctl {action} failed: {result.stderr.strip()}", err=True)
        return False
    return True




def _resolve_agent_pack(name: str, project_path: Path) -> Path | None:
    """Find a Bobi Agent source/package by name."""
    source = paths.agent_source_dir(name)
    if (source / "agent.yaml").is_file():
        return source
    cached = paths.agent_cache_dir() / name
    if (cached / "agent.yaml").is_file():
        return cached
    # Repo/deploy authoring still supports local checked-in agent packages.
    visible = project_path / "agents" / name
    if (visible / "agent.yaml").is_file():
        return visible
    return None


def _list_agent_packs(project_path: Path) -> list[tuple[str, str]]:
    """List available agent teams with their source."""
    packs: dict[str, str] = {}
    for agents_dir, label in [
        (paths.agent_cache_dir(), "cached"),
        (paths.agents_root(), "installed"),
        (project_path / "agents", "local"),
    ]:
        if agents_dir.is_dir():
            for d in sorted(agents_dir.iterdir()):
                if (d / "agent.yaml").is_file() or (d / "src" / "agent.yaml").is_file():
                    packs[d.name] = label
    return [(name, source) for name, source in sorted(packs.items())]



def _run_from_config(project_path: Path, cfg: "Config",
                     extra_subscribe: list[str] | None = None,
                     foreground: bool = False) -> None:
    """Start an agent from a Config object.

    When *foreground* is True the process is running as PID 1 in a
    container: logs go to stdout/stderr, the health endpoint is started,
    and SIGTERM triggers a graceful shutdown within the container's grace
    period.
    """
    from bobi.service import run_manager_from_config
    return run_manager_from_config(
        project_path, cfg, extra_subscribe=extra_subscribe, foreground=foreground
    )

    import atexit
    import signal
    import threading

    from bobi.sdk import set_project_root
    set_project_root(project_path)

    # Select the team's agent brain (#485) for this process and its subagents.
    from bobi.brain import set_process_brain
    set_process_brain(cfg.brain_kind, cfg.brain_model)

    agent_name = cfg.agent
    role = cfg.entry_point or "manager"

    from bobi.events.subscriptions import discover_subscriptions
    subscribe = discover_subscriptions(project_path)
    subscribe += [s for s in (extra_subscribe or []) if s not in subscribe]

    # Subscribe to every effective monitor's event topic so the coordinator
    # receives monitor findings regardless of adapter configuration. Current
    # event servers route a posted finding onto both the bare type and the
    # source-qualified "monitor/<type>" topic; monitor_subscription_keys
    # subscribes to both forms.
    from bobi.events.subscriptions import monitor_subscription_keys
    from bobi.monitors.registry import MonitorRegistry
    monitor_events = [
        m.event for m in MonitorRegistry.load(project_path=project_path).effective_monitors()
    ]
    for key in monitor_subscription_keys(monitor_events):
        if key not in subscribe:
            subscribe.append(key)

    # Subscribe to sub-agent lifecycle topics so a detached agent's
    # completion/failure is delivered back to this entry point (MDS-65 RC#1).
    # Without this, _emit_session_finished posts session.completed/failed that
    # nothing consumes — completions reach the launcher only via blocking
    # --wait, which pins a concurrency slot. Lifecycle events are delivered to
    # the inbox like monitor findings; they are never an auto-dispatch trigger.
    from bobi.events.subscriptions import lifecycle_subscription_keys
    for key in lifecycle_subscription_keys():
        if key not in subscribe:
            subscribe.append(key)

    state_dir = paths.state_dir(project_path)

    from bobi.state_version import ensure_state_version
    ensure_state_version(project_path)

    pid_str = str(os.getpid())
    (state_dir / "manager.pid").write_text(pid_str)

    def _cleanup():
        pid_file = state_dir / "manager.pid"
        try:
            if pid_file.exists() and pid_file.read_text().strip() == pid_str:
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        from bobi import manager_health
        manager_health.stop()
        from bobi import http as pooled_http
        pooled_http.close()
    atexit.register(_cleanup)

    log = logging.getLogger(__name__)

    def _handle_term(signum, frame):
        log.info("Received SIGTERM — shutting down gracefully")
        _cleanup()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_term)

    # --- Health endpoint (always started; essential in foreground/container
    # mode for liveness probes, useful in daemon mode for doctor checks) ---
    from bobi import manager_health
    health_port = manager_health.start(
        state_dir, paths.agent_name_for_root(project_path),
        manager_session=_manager_session_name(project_path, role),
    )
    log.info("Manager health endpoint on port %d", health_port)

    # --- Agent UI (opt-in via BOBI_UI) — a daemon-thread web dashboard
    # for chatting with the live team. Binds the Fly 6PN address so an operator
    # reaches it with `fly proxy`; never started unless explicitly enabled, so
    # a plain local start opens no extra port. ---
    if os.environ.get("BOBI_UI"):
        try:
            from bobi.agentui import server as agentui_server
            ui_port = agentui_server.start_in_thread(project_path,
                                                     state_dir=state_dir)
            log.info("Agent UI on port %d (reach it with `fly proxy`)", ui_port)
        except Exception as e:  # never let the UI take down the manager
            log.warning("Agent UI failed to start: %s", e)

    log.info("Bobi starting for %s (role=%s)",
             paths.agent_name_for_root(project_path), role)

    has_monitors = (
        paths.monitors_dir(project_path).is_dir()
        or cfg.monitors
    )
    if has_monitors:
        from bobi.monitors.scheduler import MonitorScheduler
        monitor_scheduler = MonitorScheduler(project_path=project_path)
        monitor_scheduler.start()
        log.info("Monitor scheduler started")

    from bobi.prompts.resolver import build_startup_prompt
    from bobi.subagent import spawn_adhoc

    session_name = _manager_session_name(project_path, role)
    task = build_startup_prompt(role, project_path, agent_name=agent_name,
                                session_name=session_name)

    # Dead-man reconcile on wake (MDS-65 §4.6): close any run stranded while no
    # manager was alive — a crash recorded with no terminal event, a swallowed
    # completion POST, or a run past its deadline. Each closed run re-emits an
    # honest agent/session.{completed,failed} that the manager (subscribed just
    # above) receives in its inbox, so the requester's thread is closed instead
    # of hanging. Best-effort: a reconcile failure must never block startup.
    try:
        from bobi.reconcile import reconcile_sessions
        # Exclude this manager's own session: the previous manager process's
        # exit is not a sub-agent failure, and the new manager re-claims the
        # entry just below.
        reconciled = reconcile_sessions(exclude_names={session_name})
        if reconciled:
            log.info("Reconciled %d stranded run(s) on startup: %s",
                     len(reconciled), [r["name"] for r in reconciled])
    except Exception:
        log.debug("Startup reconcile failed", exc_info=True)

    log.info("Bobi running for %s", paths.agent_name_for_root(project_path))
    # The manager Session subscribes to inbox/<self> (always-on) plus the
    # discovered external resource + monitor topics. One deployment, one cursor.
    spawn_adhoc(
        cwd=str(project_path),
        task=task,
        name=session_name,
        persistent=True,
        role=role,
        mcp_servers=cfg.mcp_servers or None,
        subscribe=subscribe,
    )


@main.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in the foreground (default: daemonize)")
@click.option("--fresh", is_flag=True, help="Wipe session and start clean")
@click.option("--subscribe", multiple=True, help="Additional subscriptions (e.g. linear:MOD)")
def start(foreground, fresh, subscribe):
    """Start the selected Bobi Agent.

    Reads the installed agent config from run/package/agent.yaml. If no
    agent is installed, run `bobi agents install <path> --name <name>` first.

    Usage:
        bobi agent eng start
        bobi agent eng start --foreground
        bobi agent eng start --subscribe linear:MOD
    """
    from bobi.service import (
        AlreadyRunning,
        LaunchTimeout,
        NestedRuntimeError,
        NoAgentInstalled,
        PreflightFailed,
        run_team_foreground,
        spawn_team,
    )

    project_path = _detect_project_root()

    click.echo("Running preflight checks...")
    try:
        if foreground:
            root = logging.getLogger()
            root.handlers = [
                h for h in root.handlers if not isinstance(h, logging.FileHandler)
            ]
            run_team_foreground(project_path, fresh=fresh, subscribe=list(subscribe))
            return
        result = spawn_team(project_path, fresh=fresh, subscribe=list(subscribe))
    except NoAgentInstalled as exc:
        click.echo("No agent installed. Run `bobi agents install <path> --name <name>` first.", err=True)
        if exc.available:
            click.echo("Available packs to install:", err=True)
            for name, source in exc.available:
                click.echo(f"  {name:20s} [{source}]", err=True)
        raise SystemExit(1)
    except PreflightFailed as exc:
        validation = exc.validation
        click.echo("Preflight:")
        click.echo(validation.format())
        click.echo("\nStartup blocked — fix the issues above.", err=True)
        raise SystemExit(1)
    except AlreadyRunning as exc:
        click.echo(
            f"Already running (pid {exc.pid}). "
            f"Use `bobi agent {paths.agent_name_for_root(project_path)} restart`."
        )
        return
    except NestedRuntimeError as exc:
        click.echo(
            f"A manager is already running at {exc.ancestor} (pid {exc.pid}). "
            f"Sub-agents in {paths.agent_name_for_root(project_path)} will register with that runtime. "
            f"Stop the ancestor first if you need an independent instance here.",
            err=True,
        )
        raise SystemExit(1)
    except LaunchTimeout as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1)

    validation = result.validation
    if getattr(validation, "checks", None):
        click.echo("Preflight:")
        click.echo(validation.format())
        degraded = [c for c in validation.checks if not c.ok and not c.required]
        if degraded:
            names = ", ".join(c.name for c in degraded)
            click.echo(
                f"\nStarting in degraded mode — optional services unavailable "
                f"until configured: {names}.",
                err=True,
            )
    if fresh:
        click.echo("Cleared manager session — starting fresh.")
    elif result.image_rotated:
        click.echo("Installed image changed — rotating session.")
    _print_startup_info(project_path, result.startup.pid, result.startup.log_file)


@main.command(context_settings={"ignore_unknown_options": True})
@click.argument("start_args", nargs=-1, type=click.UNPROCESSED)
def supervise(start_args):
    """Run the manager under the self-heal watchdog (#464).

    Spawns `bobi agent <name> start <args>` as a child, polls the manager health
    endpoint, and restarts a wedged director with bounded retry, backoff, loud
    logging and fail-open safety — the one recovery layer below the director
    that `stall-recovery` (director→engineer) structurally cannot provide.

    Used as the container entrypoint:

        BOBI_ROOT=<run-root> bobi supervise -- --foreground

    Everything after `--` is forwarded verbatim to the selected agent's start command (the
    `--foreground` flag is required so the manager stays a supervisable child
    and does not daemonize out from under the supervisor). Tunables are env
    vars (WATCHDOG_*); on restart-budget exhaustion the supervisor exits
    non-zero so Fly's machine restart policy escalates.
    """
    from bobi.watchdog import Supervisor, WatchdogConfig

    project_path = _detect_project_root()
    # click consumes the `--` separator, but strip a stray one defensively.
    args = [a for a in start_args if a != "--"]

    # Container/foreground mode: log to stdout/stderr only (mirror `start`).
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers
                     if not isinstance(h, logging.FileHandler)]

    log = logging.getLogger(__name__)
    log.info("watchdog: supervising selected agent start %s", " ".join(args))
    supervisor = Supervisor(args, WatchdogConfig.from_env(),
                            project_root=project_path)
    raise SystemExit(supervisor.run())


def _materialize_local_deps(pack_dir: Path, project_path: Path, *,
                            non_interactive: bool) -> None:
    """Drive the local brain to install the team's declared deps (#428 Stage 5).

    The `--with-deps` post-compose pass: resolve the team's full dependency set,
    verify what's already satisfied (idempotent skip), preview a plan, confirm,
    then materialize the rest on THIS host under the team's brain. `host:`
    capabilities are surfaced as a guided fix, never attempted, and sudo is only
    used behind an explicit confirm. Partial failure is non-fatal — doctor and
    the dispatch preflight still gate — so this never raises into install.
    """
    from bobi import local_deps
    from bobi.brain import DEFAULT_BRAIN
    from bobi.build_render import _workspace_root
    from bobi.config import Config
    from bobi.env import child_agent_env
    from bobi.host_caps import host_caps_for_deps
    from bobi.tool_library import resolve_team_dependencies

    click.echo()
    try:
        deps = resolve_team_dependencies(pack_dir, _workspace_root(pack_dir))
    except Exception as e:  # noqa: BLE001 — a dep-resolution failure is non-fatal
        click.echo(f"Could not resolve dependencies (--with-deps skipped): {e}",
                   err=True)
        return
    if not deps:
        click.echo("--with-deps: this team declares no dependencies.")
        return

    # The team's declared brain drives the install, else the local default.
    try:
        brain = Config.load(project_path).brain_kind() or DEFAULT_BRAIN
    except Exception:
        brain = DEFAULT_BRAIN

    # Bind the installed runtime so the brain session + `success` checks resolve
    # this team's paths and credentials (its run/.env).
    paths.bind_root(project_path)
    base_env = child_agent_env(project_path)

    plan = local_deps.plan_dependencies(deps, brain=brain, base_env=base_env)
    unmet_caps = [c for c in host_caps_for_deps(deps) if c.satisfied() is False]

    click.echo(f"Dependency check (brain: {brain}):")
    for dp in plan.satisfied:
        click.echo(f"  [ok]   {dp.dep.name} — already satisfied, skipping")
    for dp in plan.todo:
        sudo = " (may need sudo)" if dp.needs_sudo else ""
        click.echo(f"  [todo] {dp.dep.name} — will materialize{sudo}")
    for dp in plan.unmaterializable:
        click.echo(f"  [warn] {dp.dep.name} — unsatisfied but has no install/"
                   f"guide to materialize from; fix manually")
    for cap in unmet_caps:
        click.echo(f"  [host] {cap.spec} — host capability, provision manually: "
                   f"`{cap.fix_command()}`")

    if not plan.todo:
        click.echo("Nothing to install.")
        return

    if not non_interactive and not click.confirm(
            f"\nInstall {len(plan.todo)} dependency(ies) on this machine?",
            default=True):
        click.echo("Skipped dependency materialization.")
        return

    allow_sudo = False
    if plan.needs_sudo:
        if non_interactive:
            click.echo("Some steps may need sudo; skipping sudo "
                       "(non-interactive). Re-run interactively to allow it.")
        else:
            allow_sudo = click.confirm(
                "Some steps may require sudo (system packages). Allow sudo?",
                default=False)

    results = local_deps.install_dependencies(
        plan.todo, brain=brain, allow_sudo=allow_sudo, base_env=base_env)

    click.echo("\nDependency materialization:")
    for r in results:
        glyph = "ok" if r.ok else "FAIL"
        click.echo(f"  [{glyph}] {r.dep}"
                   + (f" — {r.detail}" if r.detail and not r.ok else ""))
        for cmd in r.transcript:
            click.echo(f"         ran: {cmd}")
    failed = [r.dep for r in results if not r.ok]
    if failed:
        slot = paths.agent_name_for_root(project_path)
        click.echo(f"\n{len(failed)} dependency(ies) not satisfied: "
                   f"{', '.join(failed)}. The team still installed; fix these "
                   f"and re-run `bobi agents install ... --with-deps`, or "
                   f"`bobi agent {slot} doctor`.", err=True)


@main.command("login-bootstrap")
@click.option("--channel", default=None,
              help="Private Slack channel ID to post the login URL into "
                   "(default: $BOBI_LOGIN_CHANNEL).")
@click.option("--timeout", default=600, type=int,
              help="Seconds to wait for the pasted auth code (default: 600).")
def login_bootstrap(channel, timeout):
    """Bootstrap subscription auth over Slack + the event bus.

    For BOBI_AUTH=subscription first boot with no credentials on the
    volume: drive `claude auth login --claudeai` under a pty, post the OAuth
    URL to a private Slack channel, and wait for the pasted code to arrive as
    a Slack event over the event bus. Idempotent — a no-op if credentials
    already exist. Fallback: `fly ssh console` then `claude auth login`.
    """
    from bobi import auth_bootstrap
    project_path = _detect_project_root()

    if auth_bootstrap.credentials_exist():
        click.echo("Subscription credentials already present — nothing to do.")
        return
    try:
        ok = auth_bootstrap.run_bootstrap(
            project_path, channel=channel, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — surface a clean CLI error
        click.echo(f"Login bootstrap failed: {exc}", err=True)
        raise SystemExit(1)
    if not ok:
        click.echo("Login bootstrap did not produce credentials.", err=True)
        raise SystemExit(1)
    click.echo("Subscription login complete.")


@main.command()
@click.argument("pack")
@click.option("--name", "slot_name", default=None,
              help="Installed Bobi Agent slot name (defaults to package name).")
@click.option("--non-interactive", is_flag=True,
              help="Skip prompts; read secrets from the environment. "
                   "Suitable for container entrypoints and CI.")
@click.option("--pinned", is_flag=True,
              help="Resolve any `from:` base teams registry-only at locked "
                   "versions (ignore local sibling checkouts). For "
                   "reproducible CI/deploy installs.")
@click.option("--with-deps", "with_deps", is_flag=True,
              help="After composing, drive the local brain to install the "
                   "team's declared dependencies on THIS machine (#428): each "
                   "dependency's `success` is verified, already-satisfied ones "
                   "are skipped, and nothing runs sudo without an explicit "
                   "confirm. Mutates the host — previews a plan and confirms "
                   "first.")
def install(pack, slot_name, non_interactive, pinned, with_deps):
    """Install a Bobi Agent into the machine-wide Bobi home.

    PACK is a local directory path, a local `.tar.gz` archive, a public
    `.tar.gz` URL, or a name to fetch from a remote registry.

    Resolution order:
      1. URL (http/https) → fetch a team archive directly
      2. Local `.tar.gz`/`.tgz` file → extract + install
      3. Local directory path (absolute or relative)
      4. Remote registry lookup by name

    Usage:
        bobi agents install agents/eng-team --name eng
        bobi agents install /path/to/my-agent --name eng
        bobi agents install ./eng-team.tar.gz --name eng
        bobi agents install https://example.com/eng-team.tar.gz --name eng
        bobi agents install eng-team --name eng
        bobi agents install eng-team --name eng --non-interactive
    """
    project_path = paths.home_dir()

    pack_str = str(pack)
    if pack_str.startswith(("http://", "https://")):
        # Public URL → fetch a team .tar.gz directly (the container first-boot /
        # CI injection seam). The installed copy is the source of truth.
        from bobi.registry import fetch_from_url
        try:
            click.echo(f"'{pack}' is a URL, fetching team archive...")
            pack_dir, _ = fetch_from_url(project_path, pack_str)
        except Exception as e:
            click.echo(f"Failed to fetch '{pack}': {e}", err=True)
            raise SystemExit(1)
    elif pack_str.endswith((".tar.gz", ".tgz")) and Path(pack).is_file():
        # Local team archive → extract + install (the ssh-push delivery seam:
        # `bobi deploy` pushes a tarball onto the instance, which installs
        # it from the volume). The installed copy is the source of truth.
        from bobi.registry import fetch_from_archive
        try:
            click.echo(f"'{pack}' is a local archive, extracting team...")
            pack_dir, _ = fetch_from_archive(project_path, Path(pack).resolve())
        except Exception as e:
            click.echo(f"Failed to install '{pack}': {e}", err=True)
            raise SystemExit(1)
    elif (pack_path := Path(pack).resolve()).is_dir() and (pack_path / "agent.yaml").exists():
        pack_dir = pack_path
    else:
        # Try remote registry. A trailing `@version` pins an immutable per-team
        # asset (D-6: split on the last `@`); a bare name takes latest. The `@`
        # is meaningful ONLY here — the URL / local-archive / local-dir branches
        # above never split on it.
        from bobi.registry import fetch, split_team_ref
        name, version = split_team_ref(pack_str)
        try:
            label = f"{name}@{version}" if version else name
            click.echo(f"'{pack}' is not a local team directory, fetching "
                       f"{label} from remote...")
            fetch(project_path, name, version=version)
            resolved = _resolve_agent_pack(name, project_path)
            if not resolved:
                click.echo(f"Failed to fetch '{pack}' from remote registries.", err=True)
                raise SystemExit(1)
            pack_dir = resolved
        except SystemExit:
            raise
        except Exception as e:
            click.echo(f"Failed to fetch '{pack}': {e}", err=True)
            raise SystemExit(1)

    agent_name = slot_name or pack_dir.name
    project_path = paths.agent_run_root(agent_name)
    project_path.mkdir(parents=True, exist_ok=True)
    paths.package_dir(project_path).mkdir(parents=True, exist_ok=True)
    paths.workspace_dir(project_path).mkdir(parents=True, exist_ok=True)

    # Local source of truth: the team source is user-authored and the installed
    # package is a generated build artifact.
    local_source = not pack_dir.is_relative_to(paths.agent_cache_dir())

    try:
        _install_pack(pack_dir, project_path, local_source, pinned=pinned)
    except Exception as e:
        from bobi.compose import ComposeError
        if isinstance(e, ComposeError):
            click.echo(f"\n{e}", err=True)
            raise SystemExit(1)
        raise
    _write_install_gitignore(project_path, local_source)

    click.echo(f"Installed Bobi Agent '{agent_name}' into {project_path}")

    installed = paths.package_dir(project_path)
    parts = []
    for subdir in ["roles", "tools", "workflows", "monitors", "context"]:
        d = installed / subdir
        if d.is_dir():
            items = [p.name for p in d.iterdir()]
            if items:
                parts.append(f"  {subdir}: {', '.join(sorted(items))}")
    if (pack_dir / "workspace").is_dir():
        parts.append("  workspace: seeded to workspace/ (existing files kept)")
    if parts:
        click.echo("\n".join(parts))

    # Collect referenced env vars and write run/.env
    from bobi.config import find_env_var_refs, parse_env_file, write_env_file
    env_refs = find_env_var_refs(project_path)
    if env_refs:
        env_file = paths.env_path(project_path)
        existing = parse_env_file(env_file)

        click.echo()
        missing = [r for r in env_refs
                   if r.name not in existing and r.name not in os.environ]

        if non_interactive:
            # Pull values from the environment — never prompt.
            for ref in env_refs:
                if ref.name not in existing and ref.name in os.environ:
                    existing[ref.name] = os.environ[ref.name]
            # A bare ${VAR} is a required secret; ${VAR:-default} carries its
            # own fallback and is optional. Fail fast on missing required
            # secrets so a container entrypoint (`install --non-interactive
            # && start`) never marches into a broken start with empty
            # credentials.
            required_missing = [r.name for r in missing if r.required]
            optional_missing = [r.name for r in missing if not r.required]
            if required_missing:
                click.echo(
                    "Error: required secrets missing from the environment: "
                    + ", ".join(required_missing)
                    + ". Set them (e.g. `fly secrets set`) and re-run "
                    "`bobi agents install --non-interactive`.",
                    err=True)
                raise SystemExit(1)
            if optional_missing:
                click.echo(
                    "Warning: optional env vars unset: "
                    + ", ".join(optional_missing), err=True)
            write_env_file(env_file, existing)
        elif missing:
            click.echo("This agent needs credentials:")
            for ref in missing:
                hint = _ENV_VAR_HINTS.get(
                    ref.name, "" if ref.required else "optional")
                label = f"  {ref.name} ({hint})" if hint else f"  {ref.name}"
                try:
                    value = click.prompt(label, default="", show_default=False)
                except (EOFError, click.Abort):
                    value = ""
                if value:
                    existing[ref.name] = value

            write_env_file(env_file, existing)
            click.echo(f"Credentials saved to {env_file}")

    if with_deps:
        _materialize_local_deps(pack_dir, project_path,
                                non_interactive=non_interactive)

    if local_source:
        try:
            src_display = pack_dir.relative_to(project_path)
        except ValueError:
            src_display = pack_dir
        click.echo(f"\nSource of truth: {src_display}/ — edit there and reinstall to change the Bobi Agent.")
    else:
        click.echo(f"\nSource of truth: {pack_dir}/")

    click.echo(f"Run `bobi agent {agent_name} start` to launch.")


@main.command()
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
    from bobi import deploy as deploy_mod
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
    except deploy_mod.DeployError as e:
        raise click.UsageError(str(e))

    # Preflight: guide the user (or an agent) through Fly setup before we build.
    deploy_mod.preflight_fly_or_exit()

    click.echo(
        f"Deploying '{name}' → app {cfg.app_name} "
        f"(fleet {cfg.fleet_stamp}, {cfg.delivery} delivery)"
    )
    try:
        deploy_mod.deploy(project_path, name, overrides)
    except deploy_mod.DeployError as e:
        click.echo(f"Deploy failed: {e}", err=True)
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        click.echo(f"Deploy failed (exit {e.returncode}).", err=True)
        raise SystemExit(1)
    click.echo(f"Deployed '{name}' (app {cfg.app_name}).")


@main.command("deploy-init")
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
    from bobi import scaffold as scaffold_mod
    from bobi.deploy import _bobi_version, DeployError

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
    except DeployError:
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


@main.command()
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
    from bobi import deploy as deploy_mod
    deploy_mod.preflight_fly_or_exit()

    overrides = {"fleet": fleet} if fleet else None
    try:
        app = deploy_mod.destroy(Path.cwd(), name, overrides, assume_yes=yes)
    except deploy_mod.DeployError as e:
        raise click.UsageError(str(e))
    except subprocess.CalledProcessError as e:
        click.echo(f"Destroy failed (exit {e.returncode}).", err=True)
        raise SystemExit(1)
    click.echo(f"Destroyed '{name}' (app {app}).")


@main.command()
@click.argument("name", required=False)
@click.option("--model", default=None,
              help="Model for the setup session (alias or full ID).")
@click.option("--resume", is_flag=True, help="Resume an interrupted setup.")
def setup(name, model, resume):
    """Interactively design, build, and install an agent team.

    Opens a local web UI (on 127.0.0.1) that goes from an idea to a
    runnable agent team: describe what you want, let bobi suggest what
    it can do on its own, connect services, watch it build the pack, then
    review and install. Interrupt anytime — `--resume` picks up where you
    left off.
    """
    agent_name = name or "new-agent"
    project_path = paths.agent_run_root(agent_name)
    project_path.mkdir(parents=True, exist_ok=True)
    paths.workspace_dir(project_path).mkdir(parents=True, exist_ok=True)

    # Setup is often the very first command a new user runs — fail with
    # a pointed message instead of a spawn error deep in the SDK.
    import shutil as _shutil
    from bobi.sdk import get_cli_path
    if not _shutil.which("claude") and not Path(get_cli_path()).exists():
        raise click.UsageError(
            "the Claude Code CLI is required for setup — install it first "
            "(https://claude.com/claude-code), then re-run `bobi setup`.")

    if not resume:
        from bobi.setup.state import SetupState
        from bobi.setup.actions import installed_team_name

        in_progress = SetupState.load(project_path)
        if in_progress and not in_progress.finished:
            click.confirm(
                f"An interrupted setup exists (stage: {in_progress.stage.value}) "
                "— resume it with `bobi setup --resume`. Start over "
                "and discard it?", abort=True)

        name = installed_team_name(project_path)
        if name:
            click.confirm(
                f"'{name}' is already installed here — setup can replace it. "
                "Continue?", abort=True)
        if not in_progress and agent_name:
            state = SetupState(team_name=agent_name)
            state.save(project_path)

    from bobi.setup import run_setup
    raise SystemExit(run_setup(project_path, model=model, resume=resume))


@main.group("app")
def app_group():
    """Manage the Bobi web app (dashboard for all your agents)."""


@app_group.command("start")
@click.option("--no-browser", is_flag=True, help="Don't open a browser.")
def app_start(no_browser):
    """Start the web app in the background (idempotent)."""
    from bobi.webapp import daemon

    try:
        st = daemon.start(open_browser=not no_browser)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    click.echo(f"bobi app is running at {st.url} (pid {st.pid})")


@app_group.command("stop")
def app_stop():
    """Stop the web app daemon."""
    from bobi.webapp import daemon

    st = daemon.stop()
    click.echo(f"Stopped (pid {st.pid})." if st.pid else "Not running.")


@app_group.command("restart")
def app_restart():
    """Restart the web app daemon."""
    from bobi.webapp import daemon

    daemon.stop()
    try:
        st = daemon.start(open_browser=False)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    click.echo(f"bobi app is running at {st.url} (pid {st.pid})")


@app_group.command("status")
def app_status():
    """Show whether the web app daemon is running."""
    from bobi.webapp import daemon

    st = daemon.status()
    if st.running:
        click.echo(f"Running at {st.url} (pid {st.pid})")
    else:
        click.echo("Not running. Start it with `bobi app start`.")
        raise SystemExit(1)


@app_group.command("run", hidden=True)
def app_run():
    """Run the web app server in the foreground (the daemon child)."""
    from bobi.webapp import daemon

    raise SystemExit(daemon.run_foreground())


@main.command()
@click.argument("name", required=False)
@click.option("--app", default=None,
              help="Target Fly app directly (skip deployment-name resolution).")
@click.option("--port", "local_port", default=None, type=int,
              help="Local port for the tunnel (default: the remote UI port).")
@click.option("--remote-port", default=None, type=int,
              help="UI port inside the container (default: read from the instance, else 8080).")
@click.option("--no-browser", is_flag=True, help="Don't open a browser window.")
@click.option("--check", is_flag=True,
              help="Remote: probe /api/agents through the tunnel once and exit (a smoke check).")
@click.pass_context
def ui(ctx, name, app, local_port, remote_port, no_browser, check):
    """View and chat with an agent team's agents in a web UI.

    \b
    Local agent:  bobi agent eng ui
    Deployed:     bobi agent eng ui <deployment>      # tunnels in via `fly proxy`
                  bobi agent eng ui --app my-bobi-eng

    Local mode serves a card per active agent on 127.0.0.1 and talks to the
    running team over the event server (so the team must already be started).
    Remote mode resolves the Fly app, reads the UI port + token off the machine,
    starts `fly proxy`, and opens the browser. Ctrl-C to stop.
    """
    # Remote mode: a deployment name or --app means "tunnel to a Fly instance".
    if name or app:
        from bobi.agentui import remote
        raise SystemExit(remote.run(
            name=name, app=app, local_port=local_port,
            remote_port=remote_port, open_browser=not no_browser, check=check))

    # Local mode: bind the registry + event-server root so the cross-process
    # `deliver` behind the chat reaches the same team start command runs.
    selected = ""
    if ctx.parent is not None and isinstance(ctx.parent.obj, dict):
        selected = str(ctx.parent.obj.get("agent") or "")
    project_path = _bind_agent_runtime(selected) if selected else _detect_project_root()
    from bobi.sdk import set_project_root
    set_project_root(project_path)
    from bobi.agentui import server as agentui_server
    raise SystemExit(agentui_server.serve(
        project_path, mode="local", open_browser=not no_browser))


def _manager_session_name(project_path: Path, role: str | None = None) -> str:
    """Session name of the project's entry-point agent.

    The single definition of the manager naming convention — start, --fresh,
    and transcript lookup all resolve the same name through here.
    """
    from bobi.service import manager_session_name
    return manager_session_name(project_path, role)


def _clear_manager_session(project_path: Path) -> None:
    """Wipe saved session ID so the manager starts a fresh conversation.

    Also drops the bubble credential and per-session deployment/cursor state:
    a fresh start mints a NEW bubble, and keeping stale deployment_state (whose
    api_key points at a now-orphaned deployment in the old bubble) would split
    the restarted sessions across bubbles.
    """
    from bobi.service import clear_manager_session
    clear_manager_session(project_path)
    click.echo("Cleared manager session — starting fresh.")


def _find_pid_path() -> Path | None:
    """Find the PID file for the selected Bobi Agent's manager."""
    project_path = _detect_project_root()
    if project_path:
        p = _project_state_dir(project_path) / "manager.pid"
        if p.exists():
            return p
    return None


def _stop_manager_pid(pid_path: Path, force: bool) -> None:
    """Kill the manager process at pid_path."""
    import signal
    import time

    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        click.echo("Invalid PID file — cleaning up.")
        pid_path.unlink(missing_ok=True)
        return

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        click.echo(f"Process {pid} not found — cleaning up stale PID file.")
        pid_path.unlink(missing_ok=True)
        return
    except PermissionError:
        click.echo(f"No permission to signal process {pid}.", err=True)
        return

    sig = signal.SIGKILL if force else signal.SIGTERM
    click.echo(f"Stopping bobi (pid {pid})...")
    os.kill(pid, sig)

    for _ in range(30):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            click.echo("Stopped.")
            return

    if not force:
        click.echo("Process didn't exit — try: bobi agent <name> stop --force")
    else:
        pid_path.unlink(missing_ok=True)
        click.echo("Killed.")


@main.command()
@click.option("--force", is_flag=True, help="Send SIGKILL if SIGTERM doesn't work")
def stop(force):
    """Stop the selected Bobi Agent.

    Usage:
        bobi agent eng stop
        bobi agent eng stop --force
    """
    if _has_systemd_service() and not force:
        click.echo("Stopping via systemd...")
        _systemctl("stop")
        return

    project_path = _ensure_root_bound()
    from bobi.service import stop_team

    result = stop_team(project_path, force=force)
    if result.invalid_pid:
        click.echo("Invalid PID file — cleaning up.")
    elif result.stale:
        click.echo(f"Process {result.pid} not found — cleaning up stale PID file.")
    elif result.permission_denied:
        click.echo(f"No permission to signal process {result.pid}.", err=True)
    elif result.stopped:
        click.echo(f"Stopping bobi (pid {result.pid})...")
        click.echo("Stopped.")
    elif result.killed:
        click.echo(f"Stopping bobi (pid {result.pid})...")
        click.echo("Killed.")
    elif result.still_running:
        click.echo(f"Stopping bobi (pid {result.pid})...")
        click.echo("Process didn't exit — try: bobi agent <name> stop --force")
    else:
        click.echo("No PID file found — bobi is not running.")

    if result.event_server_running:
        click.echo(
            f"Event server is still running on port {result.event_server_port}. "
            "Use `bobi agent <name> event-server stop` to stop it."
        )


@main.command()
@click.option("--fresh", is_flag=True, help="Wipe manager session and start clean")
def restart(fresh):
    """Stop and restart the selected Bobi Agent.

    Usage:
        bobi agent eng restart
        bobi agent eng restart --fresh   # fresh manager session
    """
    if _has_systemd_service():
        # Resolve before touching systemd so a missing installation fails
        # here, not after the service has already been restarted.
        project_path = _detect_project_root()
        if fresh:
            _clear_manager_session(project_path)
        click.echo("Restarting via systemd...")
        _systemctl("restart")
        result = subprocess.run(
            ["systemctl", "--user", "show", "bobi", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        pid = result.stdout.strip()
        log_path = _project_state_dir(project_path) / "manager.log"
        click.echo(f"Bobi restarted (pid {pid}). Logs: {log_path}")
        return

    ctx = click.get_current_context()
    ctx.invoke(stop)
    ctx.invoke(start, fresh=fresh)


def _resolve_address(to: str | None) -> str | None:
    """Resolve a friendly address to a session name.

    'manager' or None → finds the coordinator session by the installed
    package's entry_point role, then the literal role "manager".
    Anything else → used as-is (exact session name).
    """
    project_path = _detect_project_root()
    from bobi.service import resolve_address
    return resolve_address(project_path, to)


@main.command()
@click.argument("text", required=True)
@click.option("--to", default=None, help="Target session (default: manager)")
@click.option("--wait", is_flag=True, help="Block until the session responds")
@click.option("--timeout", default=300, type=int, help="Timeout in seconds (with --wait)")
def message(text, to, wait, timeout):
    """Send a message to any session via its inbox.

    Usage:
        bobi agent eng message "what are you working on?"
        bobi agent eng message --to eng-42-implement "try a different approach"
        bobi agent eng message --to manager "status?" --wait
    """
    from bobi.service import MessageDeliveryError, send_message

    project_path = _detect_project_root()
    try:
        result = send_message(
            project_path, text, wait=wait, session=to, timeout=timeout, sender="cli"
        )
        if wait and result.response:
            click.echo(result.response)
        else:
            click.echo(f"Sent to {result.address}")
    except MessageDeliveryError as exc:
        msg = str(exc)
        if msg.startswith("No active session"):
            click.echo(msg, err=True)
        else:
            click.echo(f"Failed: {msg}", err=True)
        raise SystemExit(1)


@main.command()
@click.option("--to", default=None, help="Target session (default: manager)")
def compact(to):
    """Compact a session's context now — flush its decision log and rotate.

    Triggers the same graceful rotation the token cap does, on demand: the
    session writes its decision log to INDEX.md, then swaps to a fresh
    conversation that reloads only that log. Use it when a long-lived
    session has grown slow. Rotation happens at the session's next idle
    moment (it won't interrupt an in-flight turn).

    Usage:
        bobi agent eng compact                       # compact the manager
        bobi agent eng compact --to eng-42-implement # compact a specific session
    """
    from bobi.inbox import deliver
    from bobi.session import COMPACT_SENTINEL

    address = _resolve_address(to)
    if not address:
        target = to or "manager"
        click.echo(f"No active session found for '{target}'.", err=True)
        raise SystemExit(1)

    ok, response = deliver(address, COMPACT_SENTINEL, sender="cli", wait=False)
    if ok:
        click.echo(f"Compaction requested for {address} — it will flush its "
                   f"decision log and rotate at its next idle moment.")
    else:
        click.echo(f"Failed: {response}", err=True)
        raise SystemExit(1)


@main.command(hidden=True)
@click.argument("question", required=True)
@click.option("--timeout", default=300, type=int, help="Timeout in seconds")
@click.option("--source", default="engineer", help="Source identifier")
def ask(question, timeout, source):
    """Ask the manager a question (alias for: message --wait)."""
    from bobi.service import MessageDeliveryError, send_message

    project_path = _detect_project_root()
    try:
        result = send_message(
            project_path, question, wait=True, session="manager",
            timeout=timeout, sender=source,
        )
        click.echo(result.response)
    except MessageDeliveryError as exc:
        msg = str(exc)
        if msg.startswith("No active session"):
            click.echo("No active manager session found.", err=True)
        else:
            click.echo(f"Failed: {msg}", err=True)
        raise SystemExit(1)


@main.command("slack-reply")
@click.argument("text")
@click.option("--workspace", "-w", required=True, help="Slack workspace ID (e.g. T0952RZRZ0X)")
@click.option("--channel", "-c", required=True, help="Slack channel ID (e.g. D0B51JP1N4C)")
@click.option("--thread", "-t", default="", help="Thread timestamp to reply in")
@click.option("--edit", "edit_ts", default="", help="Placeholder message ts to edit instead of posting new")
def slack_reply(text, workspace, channel, thread, edit_ts):
    """Post a message to Slack. Used by the manager to reply to Slack events.

    Usage:
        bobi slack-reply -w T0952RZRZ0X -c D0B51JP1N4C "Hello"
        bobi slack-reply -w T0952RZRZ0X -c C123 -t 1780165787.159589 "Thread reply"
        bobi slack-reply -w T0952RZRZ0X -c C123 -t 171.42 --edit 171.99 "Real response"
    """
    import httpx

    from .slack import post_slack_message, update_slack_message, set_thread_status

    token = ""
    project_path = _detect_project_root()
    if project_path:
        from .config import Config
        cfg = Config.load(project_path)
        token = cfg.credential("slack", "bot_token")
    if not token:
        click.echo("No bot token configured (set credentials.bot_token under the slack service in agent.yaml)", err=True)
        sys.exit(1)

    try:
        if edit_ts:
            update_slack_message(token, channel, edit_ts, text)
            if thread:
                set_thread_status(token, channel, thread, "")
                # Stop the background refresh loop (if one is running).
                from .events.channels import stop_refresh_loop
                stop_refresh_loop(channel, thread)
            click.echo(f"Updated {edit_ts} in {channel}")
        else:
            post_slack_message(token, channel, text, thread_ts=thread)
            click.echo(f"Sent to {channel}")
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except (httpx.HTTPError, OSError, TimeoutError) as e:
        click.echo(f"Failed: {e}", err=True)
        sys.exit(1)


@main.command("slack-upload-file")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--workspace", "-w", required=True, help="Slack workspace ID (e.g. T0952RZRZ0X)")
@click.option("--channel", "-c", required=True, help="Slack channel ID")
@click.option("--thread", "-t", default="", help="Thread timestamp to upload into")
@click.option("--title", default="", help="File title in Slack")
@click.option("--comment", default="", help="Initial comment with the file")
@click.option("--filename", default="", help="Override filename (default: basename of path)")
def slack_upload_file(file_path, workspace, channel, thread, title, comment, filename):
    """Upload a file to Slack.

    Usage:
        bobi slack-upload-file ./screenshot.png -w T0952RZRZ0X -c C123
        bobi slack-upload-file ./report.pdf -w T0952RZRZ0X -c C123 -t 171.42 --title "Report"
    """
    import httpx
    from pathlib import Path

    from .slack import upload_slack_file

    token = ""
    project_path = _detect_project_root()
    if project_path:
        from .config import Config
        cfg = Config.load(project_path)
        token = cfg.credential("slack", "bot_token")
    if not token:
        click.echo("No bot token configured (set credentials.bot_token under the slack service in agent.yaml)", err=True)
        sys.exit(1)

    p = Path(file_path)
    file_data = p.read_bytes()
    fname = filename or p.name

    try:
        upload_slack_file(
            token, channel, file_data, fname,
            title=title, thread_ts=thread, initial_comment=comment,
        )
        click.echo(f"Uploaded {fname} to {channel}")
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except (httpx.HTTPError, OSError, TimeoutError) as e:
        click.echo(f"Failed: {e}", err=True)
        sys.exit(1)


@main.command("slack-read-thread")
@click.option("--workspace", "-w", required=True, help="Slack workspace ID (e.g. T0952RZRZ0X)")
@click.option("--channel", "-c", required=True, help="Slack channel ID")
@click.option("--thread", "-t", required=True, help="Thread timestamp to read")
@click.option("--limit", "-n", default=100, help="Max messages to fetch (default: 100)")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def slack_read_thread(workspace, channel, thread, limit, as_json):
    """Read all messages in a Slack thread.

    Usage:
        bobi slack-read-thread -w T0952RZRZ0X -c C123 -t 1780165787.159589
        bobi slack-read-thread -w T0952RZRZ0X -c C123 -t 171.42 --json-output
    """
    import json as _json

    import httpx

    from .slack import fetch_slack_thread

    token = ""
    project_path = _detect_project_root()
    if project_path:
        from .config import Config
        cfg = Config.load(project_path)
        token = cfg.credential("slack", "bot_token")
    if not token:
        click.echo("No bot token configured (set credentials.bot_token under the slack service in agent.yaml)", err=True)
        sys.exit(1)

    try:
        messages = fetch_slack_thread(token, channel, thread, limit=limit)
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except (httpx.HTTPError, OSError, TimeoutError) as e:
        click.echo(f"Failed: {e}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(messages, indent=2))
    else:
        for msg in messages:
            user = msg.get("user", "unknown")
            text = msg.get("text", "")
            ts = msg.get("ts", "")
            files = msg.get("files", [])
            click.echo(f"[{ts}] {user}: {text}")
            for f in files:
                name = f.get("name", "file")
                mimetype = f.get("mimetype", "")
                click.echo(f"  >> {name} ({mimetype})")
        click.echo(f"\n{len(messages)} message(s)")


@main.command("create-slack-bot")
@click.option("--app-name", default=None,
              help='Display name for the Slack app (default: "bobi agent"; '
                   "prompted when run interactively)")
@click.option("--event-server", default="",
              help="Event server base URL (default: the configured server, "
                   "else prompted interactively, else the bobi cloud)")
@click.option("--format", "fmt", type=click.Choice(["yaml", "json"]),
              default="yaml", help="Manifest output format")
@click.option("--output", "-o", "output", type=click.Path(), default="",
              help="Write the manifest to a file instead of stdout")
@click.option("--url/--no-url", "show_url", default=True,
              help="Print a one-click 'create from manifest' link")
@click.option("--open/--no-open", "open_browser", default=None,
              help="Open the one-click create link in your browser "
                   "(default: when run interactively; use --no-open for "
                   "headless/CI)")
def create_slack_bot(app_name, event_server, fmt, output, show_url, open_browser):
    """Create a Slack app (bot) for bobi — generates the manifest and a
    one-click create link, and opens it in your browser.

    Every bobi Slack app needs the same scopes + events pointed at one
    request URL; this stamps them out from a template so a working app is one
    step away — click the link it opens, feed the file to the Slack CLI
    (`slack create <name> --manifest manifest.json`), or POST it to the App
    Manifest API.

    Usage:
        bobi create-slack-bot
        bobi create-slack-bot --app-name "Eng Bot" --format json -o manifest.json
        bobi create-slack-bot --event-server https://my-worker.workers.dev
        bobi create-slack-bot --no-open                # just print the link
    """
    from .deploy import DEFAULT_EVENT_SERVER
    from .slack_manifest import (
        create_app_url, manifest_to_json, render_manifest, webhook_url,
    )

    interactive = _interactive_terminal()

    if app_name is None:
        app_name = (click.prompt("Slack app display name", default="bobi agent")
                    if interactive else "bobi agent")

    if not event_server:
        # Resolve from the project config when run inside an install; this
        # command also works before any Bobi Agent is installed, so a missing
        # root is fine.
        try:
            project_path = _detect_project_root()
        except click.UsageError:
            project_path = None
        if project_path:
            from .config import Config
            event_server = Config.load(project_path).event_server_url
    if not event_server and interactive:
        # No configured server: let the user pick before the manifest is
        # rendered and the create page opens. Slack must be able to reach
        # this URL from the internet, so a laptop running the local event
        # server needs a public tunnel in front of localhost:8080.
        click.echo("Where should Slack send events (the app's request URL)?")
        click.echo("  Press Enter to use the bobi cloud event server, or type "
                   "your own URL.")
        click.echo("  Running the agent on this machine with the local event "
                   "server? Slack can't reach localhost - put a public tunnel "
                   "(e.g. cloudflared or ngrok) in front of localhost:8080 "
                   "and enter the tunnel URL.")
        event_server = click.prompt("Event server URL",
                                    default=DEFAULT_EVENT_SERVER)
        click.echo("")
    if not event_server:
        # Non-interactive with nothing configured: the bobi cloud.
        event_server = DEFAULT_EVENT_SERVER

    manifest_yaml = render_manifest(app_name, event_server)
    rendered = manifest_to_json(manifest_yaml) if fmt == "json" else manifest_yaml

    if output:
        Path(output).write_text(rendered.rstrip("\n") + "\n")
        click.echo(f"Wrote {fmt} manifest to {output}")
    else:
        click.echo(rendered)

    if show_url:
        create_url = create_app_url(manifest_yaml)
        click.echo("")
        click.echo(f"Request URL:  {webhook_url(event_server)}")
        click.echo("Create the app in one click:")
        click.echo(f"  {create_url}")
        # Open the browser by default when interactive; --open/--no-open
        # forces either way. The default (None) stays quiet under pipes, CI,
        # and the test runner so it never tries to launch a browser there.
        should_open = (
            open_browser if open_browser is not None else sys.stdout.isatty()
        )
        if should_open:
            click.launch(create_url)
            click.echo("")
            click.echo("Opened the create page in your browser.")


@main.group()
def transcript():
    """Session transcripts — view, search, and index conversation history."""
    _ensure_root_bound()


@transcript.command("show")
@click.argument("session", default="manager")
@click.option("-n", "--lines", default=30, help="Number of recent messages to show")
@click.option("-f", "--follow", is_flag=True, help="Follow mode — stream new entries")
def transcript_show(session, lines, follow):
    """Show the transcript for a session.

    Usage:
        bobi agent eng transcript show manager        # manager transcript
        bobi agent eng transcript show eng-70         # engineer transcript
        bobi agent eng transcript show manager -n 50  # last 50 messages
        bobi agent eng transcript show manager -f     # follow mode
    """
    transcript_path = _find_transcript(session)
    if not transcript_path:
        return

    if follow:
        import time
        last_size = 0
        all_lines = transcript_path.read_text().strip().splitlines()
        for line in all_lines[-lines:]:
            _print_transcript_entry(line)
        last_size = transcript_path.stat().st_size
        try:
            while True:
                time.sleep(1)
                cur_size = transcript_path.stat().st_size
                if cur_size > last_size:
                    with open(transcript_path) as f:
                        f.seek(last_size)
                        for line in f:
                            _print_transcript_entry(line.strip())
                    last_size = cur_size
        except KeyboardInterrupt:
            pass
    else:
        all_lines = transcript_path.read_text().strip().splitlines()
        for line in all_lines[-lines:]:
            _print_transcript_entry(line)


def _find_transcript(session: str) -> Path | None:
    """Find the log file for a session."""
    from bobi.sdk import SessionRegistry, get_registry

    if session == "manager":
        project = _detect_project_root()
        session = _manager_session_name(project) if project else "bobi-manager"

    # Primary: session dir log
    session_log = SessionRegistry.log_path(session)
    if session_log.exists():
        return session_log

    # Fallback: Claude Code transcript via session ID
    from bobi.sdk import _sessions_dir
    id_file = _sessions_dir() / f"{session}.id"
    if id_file.exists():
        session_id = id_file.read_text().strip()
        if session_id:
            claude_projects = Path.home() / ".claude" / "projects"
            if claude_projects.exists():
                for project_dir in claude_projects.iterdir():
                    candidate = project_dir / f"{session_id}.jsonl"
                    if candidate.exists():
                        return candidate

    click.echo(f"No session '{session}'.")
    registry = get_registry()
    active = [e for e in registry.list_active() if e.role == "engineer"]
    if active:
        names = [e.name for e in active]
        click.echo(f"Active: {', '.join(sorted(names))}")
    sessions = _sessions_dir()
    recent_dirs = sorted(
        [d for d in sessions.iterdir() if d.is_dir() and (d / "state.json").exists()],
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    recent_names = [d.name for d in recent_dirs[:10] if not d.name.startswith("bobi-mgr")]
    if recent_names:
        click.echo(f"Recent: {', '.join(recent_names)}")
    return None


def _print_transcript_entry(line: str) -> None:
    """Render one JSONL line from a Claude Code transcript or activity log."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # Plain text lines (e.g. orchestrator print output)
        line = line.strip()
        if line:
            click.echo(f"  {line}")
        return

    # Activity log format (from orchestrator/engineer subprocess)
    event = obj.get("event", "")
    if event == "response":
        import datetime
        ts = datetime.datetime.fromtimestamp(obj.get("ts", 0)).strftime("%H:%M:%S")
        text = obj.get("text", "")[:300]
        click.echo(f"{ts}  ← {text}")
        return
    if event == "tool_use":
        import datetime
        ts = datetime.datetime.fromtimestamp(obj.get("ts", 0)).strftime("%H:%M:%S")
        tool = obj.get("tool", "")
        inp = obj.get("input", "")[:150]
        click.echo(f"{ts}  ⚙ {tool}: {inp}")
        return
    if event == "stop":
        click.echo(f"  ◼ turn complete")
        return

    # Claude Code transcript format
    msg_type = obj.get("type", "")
    ts = obj.get("timestamp", "")[:19]

    if msg_type in ("human", "user"):
        content = obj.get("message", {}).get("content", [])
        text = ""
        for part in content:
            if isinstance(part, str):
                text += part
            elif isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
        text = text.strip()
        if text:
            # Truncate long event payloads but show Slack messages in full
            display = text[:300] + "..." if len(text) > 300 else text
            click.echo(f"\n{ts}  → {display}")

    elif msg_type == "assistant":
        content = obj.get("message", {}).get("content", [])
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text", "").strip()
                if text:
                    click.echo(f"{ts}  ← {text}")
            elif part.get("type") == "tool_use":
                name = part.get("name", "")
                inp = part.get("input", {})
                if isinstance(inp, dict):
                    summary = inp.get("command", inp.get("description", str(inp)))
                else:
                    summary = str(inp)
                summary = str(summary)[:150]
                click.echo(f"{ts}  ⚙ {name}: {summary}")



@main.command()
def status():
    """Show active agents — manager + engineer sub-agents."""
    project_path = _detect_project_root()

    if not project_path:
        click.echo("No Bobi Agent runtime selected. Use `bobi agents list`, then `bobi agent <name> status`.")
        raise SystemExit(1)

    from bobi.service import team_status

    result = team_status(project_path)
    if result.manager_running:
        click.echo(f"  Agent: running (pid {result.manager_pid})")
    else:
        click.echo("  Agent: stopped")

    active = result.active_agents
    if not active:
        click.echo("  Sub-agents: none active")
        return

    click.echo(f"  Sub-agents: {len(active)} active")
    for e in active:
        rotation_info = f", rotations={e.rotation_count}" if e.rotation_count else ""
        click.echo(f"    {e.name} ({e.role}) — {e.status}{rotation_info}")


@main.command()
@click.option("--browser", is_flag=True, default=False,
              help="Also run /browse + Chromium sandbox checks")
@click.option("--fix", is_flag=True, help="Offer to apply the Chromium sandbox fix (with --browser)")
def doctor(browser, fix):
    """System health check — verify manager, event server, dashboard, repos, workflows.

    Runs a suite of checks and prints a pass/fail line for each.
    Exit 0 if all pass, 1 if any fail.

    Usage:
        bobi agent eng doctor
        bobi agent eng doctor --browser
        bobi agent eng doctor --browser --fix
    """
    from .doctor import run_doctor
    from bobi.validate import status_glyph, supports_unicode

    # Resolve the glyph set once: ✓/✗/⚠, or [OK]/[ERROR]/[WARN] on
    # unicode-stripped terminals. Shared by the no-install warning below and
    # the per-check rows further down.
    unicode = supports_unicode()
    warn_mark = status_glyph(False, False, unicode=unicode)

    # doctor is advisory and must never silently pass without a selected
    # runtime: a green report outside an installation would be a lie.
    if paths.bound_root() is None:
        click.echo(click.style(f"{warn_mark} No Bobi Agent runtime selected — "
                               "agent-scoped checks will report 'no project "
                               "detected'.", fg="yellow"))

    results = run_doctor()

    if browser:
        from . import browser as browser_mod
        if not browser_mod.is_linux():
            click.echo("Note: Chromium sandbox checks are Linux-specific; "
                       "running browser launch checks only.")
        results.extend(browser_mod.run_doctor())

    all_ok = True
    warnings = 0
    sandbox_failure = False
    for r in results:
        required = getattr(r, "required", True)
        # ✓ ok / ✗ blocking failure / ⚠ non-blocking warning (optional service),
        # with [OK]/[ERROR]/[WARN] fallback on unicode-stripped terminals.
        mark = status_glyph(r.ok, required, unicode=unicode)
        click.echo(f"  {mark} {r.name}: {r.detail}")
        if not r.ok:
            if required:
                all_ok = False
            else:
                warnings += 1
            if r.hint:
                click.echo(f"      → {r.hint}")
            if browser and hasattr(r, "sandbox_error") and r.sandbox_error:
                sandbox_failure = True

    if all_ok:
        if warnings:
            click.echo(f"\nAll required checks passed ({warnings} warning(s)).")
        else:
            click.echo("\nAll checks passed.")
        return

    if sandbox_failure and fix:
        from . import browser as browser_mod
        click.echo()
        _offer_sandbox_fix(browser_mod)
    elif sandbox_failure:
        click.echo("\nRe-run with `bobi agent <name> doctor --browser --fix` to apply the sandbox fix.")

    raise SystemExit(1)


def _offer_sandbox_fix(browser_mod) -> None:
    """Explain the Chromium sandbox issue and interactively apply the fix.

    Used by `bobi agent <name> doctor --fix`. Asks for confirmation before running
    the sudo sysctl change.
    """
    click.echo("Chromium's sandbox is blocked by the AppArmor restriction on")
    click.echo("unprivileged user namespaces — this prevents /browse from running.")
    click.echo()
    click.echo(f"  The fix:  {browser_mod.FIX_COMMAND}")
    click.echo(f"  Persisted in: {browser_mod.SYSCTL_CONF_PATH}")
    click.echo()
    click.echo("  Security tradeoff: this lets any local process create user")
    click.echo("  namespaces, a historical local-privilege-escalation surface.")
    click.echo("  Acceptable on dedicated dev machines. See scripts/install.sh for")
    click.echo("  a narrower per-binary AppArmor alternative and the --no-sandbox fallback.")
    click.echo()

    try:
        if not click.confirm("  Apply the fix now (requires sudo)?", default=False):
            click.echo("  Skipped. Apply it later with the command above.")
            return
    except (EOFError, click.Abort):
        click.echo("  Skipped.")
        return

    ok, message = browser_mod.apply_sandbox_fix(persist=True)
    if ok:
        click.echo(f"  {message}")
        recheck = browser_mod.check_chromium_launch()
        if recheck.ok:
            click.echo("  Verified — Chromium now launches. /browse is ready.")
        else:
            click.echo(f"  Applied, but Chromium still fails: {recheck.detail}")
    else:
        click.echo(f"  Fix failed: {message}", err=True)


@main.group()
def agents():
    """Installed Bobi Agent management."""
    pass


@agents.command("list")
def agents_list():
    """List installed Bobi Agents."""
    installed = paths.list_agents()
    if not installed:
        click.echo("No Bobi Agents installed.")
        return
    for name in installed:
        root = paths.agent_run_root(name)
        state = "running" if paths.manager_pid_path(root).exists() else "stopped"
        click.echo(f"  {name:24s} {state:8s} {root}")


@agent.group("subagents")
def subagents():
    """Launch, list, inspect, and cancel sub-agents."""
    pass


@subagents.command("list")
def subagents_list():
    """List active sub-agents from the selected Bobi Agent runtime."""
    _ensure_root_bound()
    from bobi.subagent import list_agents as _list_agents

    active = _list_agents()
    if not active:
        click.echo("No active sub-agents.")
        return

    for a in active:
        state = "running" if a["running"] else "done"
        label = a.get("name") or f"{a['run_key']}/{a['phase']}"
        click.echo(f"  {label} — {state} ({a['elapsed_s']}s)")


@subagents.command("show")
@click.argument("ref")
def subagents_show(ref):
    """Show details for a specific sub-agent."""
    _ensure_root_bound()
    import time as _time
    from bobi.subagent import find_agent

    entry = find_agent(ref)
    if not entry:
        click.echo(f"No sub-agent found for {ref}")
        return

    click.echo(f"  Session: {entry.name}")
    if entry.run_key:
        click.echo(f"  Run key: {entry.run_key}")
    click.echo(f"  Phase:   {entry.phase}")
    if entry.status in ("starting", "running", "idle"):
        elapsed = int(_time.time() - entry.started_at)
        click.echo(f"  Status:  {entry.status} ({elapsed}s)")
    else:
        click.echo(f"  Status:  {entry.status}")
    if entry.cwd:
        click.echo(f"  CWD:     {entry.cwd}")
    if entry.title:
        click.echo(f"  Task:    {entry.title}")


@subagents.command("cancel")
@click.argument("ref")
def subagents_cancel(ref):
    """Cancel a running sub-agent."""
    _ensure_root_bound()
    from bobi.subagent import cancel_agent

    if cancel_agent(ref):
        click.echo(f"Cancelled {ref}")
    else:
        click.echo(f"No running sub-agent for {ref}")



@main.command()
@click.argument("name", default="bobi")
def skill(name):
    """Print a skill guide to stdout.

    Agents can bootstrap themselves with: bobi skill

    Usage:
        bobi skill                # print the bobi usage guide
        bobi skill create-agent   # print the agent creation guide
        bobi skill linear-setup   # print the Linear setup guide
    """
    # In a source checkout, the repo-level skills/ directory is canonical.
    # Wheels bundle that same directory into bobi/skills/ via pyproject
    # force-include, so installed users do not need a repo checkout.
    repo_skills = _PACKAGE_DIR.parent / "skills"
    skills_dir = repo_skills if repo_skills.is_dir() else _PACKAGE_DIR / "skills"
    path = skills_dir / f"{name}.md"
    if not path.exists():
        available = [f.stem for f in skills_dir.glob("*.md")] if skills_dir.exists() else []
        click.echo(f"Skill '{name}' not found.", err=True)
        if available:
            click.echo(f"Available: {', '.join(sorted(available))}", err=True)
        raise SystemExit(1)
    click.echo(path.read_text())


def _show_events(tail: int, decisions_only: bool) -> None:
    """Show recent events and manager decisions as a unified timeline."""
    project_path = _detect_project_root()

    entries = []
    malformed = 0

    if not decisions_only:
        state_dir = paths.state_path(project_path)
        event_files = list(state_dir.glob("events-*.jsonl"))

        seen_events: set[tuple] = set()  # (seq, deployment_id) dedup

        for events_path in event_files:
            for line in events_path.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue

                # Deduplicate by (seq, deployment_id) when both are present.
                seq = entry.get("seq")
                dep = entry.get("deployment_id")
                if seq is not None and dep is not None:
                    key = (seq, dep)
                    if key in seen_events:
                        continue
                    seen_events.add(key)

                data = entry.get("payload", entry.get("data", {}))
                detail = ""
                if entry.get("source") == "inbox" and isinstance(data, dict):
                    sender = data.get("sender", data.get("from", ""))
                    text = data.get("text", "")
                    if sender and text:
                        detail = f"{sender} → {text}"
                    elif text:
                        detail = text
                if not detail:
                    detail = data.get("text", "") or data.get("title", "") or data.get("run_key", "") if isinstance(data, dict) else ""
                if len(detail) > 80:
                    detail = detail[:80] + "..."
                entries.append((
                    entry["timestamp"],
                    f"  {entry['timestamp']}  {entry['source']:8s}  {entry['type']}"
                    + (f"\n    {detail}" if detail else ""),
                ))

    decisions_path = paths.state_path(project_path) / "decisions.jsonl"
    if decisions_path.exists():
        for line in decisions_path.read_text().strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            actions = entry.get("actions", [])
            types = ", ".join(a.get("type", "?") for a in actions)
            reason = ""
            if entry.get("reasoning"):
                reason = f"\n    {entry['reasoning'][:200].replace(chr(10), ' ')}"
            entries.append((
                entry["timestamp"],
                f"  {entry['timestamp']}  decision  {types}{reason}",
            ))

    if not entries:
        click.echo("No events yet.")
        return

    entries.sort(key=lambda e: e[0])
    for _, text in entries[-tail:]:
        click.echo(text)

    if malformed:
        click.echo(f"\n  ⚠ {malformed} malformed line(s) skipped", err=True)


def _parse_event_publish_payload(json_payload: str | None) -> dict:
    if json_payload is None:
        stdin = click.get_text_stream("stdin")
        if stdin.isatty():
            raise click.UsageError("Provide payload with --json or stdin.")
        raw = stdin.read()
    else:
        raw = json_payload
    raw = raw.strip()
    if not raw:
        raise click.UsageError("Provide payload with --json or stdin.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Payload must be valid JSON: {exc.msg}.") from exc
    if not isinstance(payload, dict):
        raise click.UsageError("Payload must be a JSON object.")
    return payload


def _validate_event_publish_topic(topic: str) -> str:
    source, sep, etype = topic.partition("/")
    if not sep or not source or not etype:
        raise click.UsageError(
            "Topic must use source/type form, e.g. alert/firing."
        )
    global_prefixes = ("github:", "linear:", "slack:")
    if (
        topic.startswith(global_prefixes)
        or source.startswith(global_prefixes)
        or etype.startswith(global_prefixes)
    ):
        raise click.UsageError(
            "github:, linear:, and slack: topics are reserved for webhooks."
        )
    return topic


@main.group(invoke_without_command=True)
@click.option("--tail", default=20, help="Number of recent entries to show")
@click.option("--decisions-only", is_flag=True, help="Show only manager decisions")
@click.pass_context
def events(ctx, tail, decisions_only):
    """Show recent events and manager decisions as a unified timeline."""
    if ctx.invoked_subcommand is not None:
        return
    _show_events(tail, decisions_only)


@events.command("publish")
@click.argument("topic")
@click.option("--json", "json_payload", default=None,
              help="JSON object payload. If omitted, payload is read from stdin.")
def events_publish(topic, json_payload):
    """Publish a signed custom-topic event."""
    project_path = _detect_project_root()
    topic = _validate_event_publish_topic(topic)
    payload = _parse_event_publish_payload(json_payload)

    from bobi.events.publish import post_event
    if not post_event(topic, payload, project_path=project_path):
        click.echo(
            "Publish failed. Ensure the agent is started, bubble credentials "
            "are minted, and the event server accepted the signed publish.",
            err=True,
        )
        raise SystemExit(1)

    click.echo(f"Published {topic}")





@transcript.command("index")
@click.option("--project", default=None, help="Filter to project (substring match on path)")
def transcript_index(project):
    """Index conversation JSONL files into searchable SQLite.

    Scans ~/.claude/projects/*/conversations/ for JSONL files and indexes
    messages into a local SQLite database for fast searching.

    Usage:
        bobi agent eng transcript index                # index all projects
        bobi agent eng transcript index --project foo  # index only projects matching "foo"
    """
    from .history import index as do_index
    click.echo("Indexing conversations...")
    stats = do_index(project_filter=project)
    click.echo(f"  Scanned {stats['files_scanned']} files, {stats['files_with_new']} had new data")
    click.echo(f"  Indexed {stats['new_messages']} new messages")
    click.echo(f"  Total: {stats['total_conversations']} conversations, {stats['total_messages']} messages")


@transcript.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Max results")
@click.option("--project", default=None, help="Filter to project")
def transcript_search(query, limit, project):
    """Full-text search across indexed conversation history.

    Searches message content using SQLite FTS. Requires
    `bobi agent <name> transcript index` to have been run first.

    Usage:
        bobi agent eng transcript search "error handling"
        bobi agent eng transcript search "deploy" --project bobi --limit 5
    """
    from .history import search as do_search
    results = do_search(query, limit=limit, project=project)
    if not results:
        click.echo("No results. Run `bobi agent <name> transcript index` first.")
        return
    for r in results:
        branch = r.get("git_branch") or ""
        role = r.get("role") or r.get("type") or ""
        tool = f" [{r['tool_name']}]" if r.get("tool_name") else ""
        snippet = (r.get("snippet") or "")[:200].replace("\n", " ")
        click.echo(f"  {r['timestamp'][:19]}  {role:10s}{tool}  {branch}")
        click.echo(f"    {snippet}")
        click.echo()


@transcript.command("sessions")
@click.option("--limit", default=20)
@click.option("--project", default=None)
def transcript_sessions(limit, project):
    """List indexed conversations with metadata.

    Shows session ID, git branch, message count, and working directory for
    each indexed conversation.

    Usage:
        bobi agent eng transcript sessions
        bobi agent eng transcript sessions --limit 5 --project bobi
    """
    from .history import conversations
    convos = conversations(limit=limit, project=project)
    if not convos:
        click.echo("No conversations indexed. Run `bobi agent <name> transcript index` first.")
        return
    for c in convos:
        branch = c.get("git_branch") or ""
        click.echo(f"  {c['started_at'][:19]}  {c['session_id'][:8]}  {branch:20s}  {c['message_count']} msgs  {c.get('cwd', '')}")


@transcript.command("inspect")
@click.argument("session_id")
@click.option("--limit", default=50)
def transcript_inspect(session_id, limit):
    """Show messages from an indexed session.

    Accepts a full or partial session ID (prefix match). Use
    `bobi agent <name> transcript sessions` to find session IDs.

    Usage:
        bobi agent eng transcript inspect abc12345
        bobi agent eng transcript inspect abc12345 --limit 10
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


@main.group()
def workflows():
    """Workflow engine — manage YAML-based DAG workflows."""
    pass


@workflows.command("list")
def workflow_list():
    """List available workflow definitions from the installed pack.

    Usage:
        bobi agent eng workflows list
    """
    from .workflow.triggers import WorkflowDispatcher

    project_path = _try_detect_project_root()
    dispatcher = WorkflowDispatcher()
    if project_path is not None:
        dispatcher.load_all_workflows(project_path)
    click.echo(dispatcher.format_workflow_menu())


@workflows.command("status")
def workflow_status():
    """Show active and recent workflow runs.

    Displays up to 20 recent runs with their status, trigger issue,
    current step, and start time.

    Usage:
        bobi agent eng workflows status
    """
    _ensure_root_bound()
    from .workflow.state import WorkflowRun
    runs = WorkflowRun.list_runs()
    if not runs:
        click.echo("No workflow runs found.")
        return
    for run in runs[:20]:
        event_data = run.trigger_event.get("data", {})
        issue = event_data.get("run_key", run.run_key or "?")
        suffix = ""
        if run.suspended_at_step >= 0:
            suffix = f"  step={run.suspended_at_step}"
        if run.status == "waiting" and run.await_event:
            suffix += f"  awaiting={run.await_event}"
        click.echo(f"  {run.run_id}  {run.workflow_name:20s} {run.status:10s} "
                  f"issue={issue}  {run.started_at[:19]}{suffix}")


@workflows.command("resume")
@click.argument("run_id")
@click.option("--timeout", default=3600, help="Max execution time in seconds")
def workflow_resume(run_id, timeout):
    """Resume a suspended workflow run.

    Picks up from the step after the await that suspended it.

    Usage:
        bobi agent eng workflows resume abc123
    """
    _ensure_root_bound()
    from .workflow.state import WorkflowRun
    from .workflow.triggers import WorkflowDispatcher
    from .workflow.orchestrator import resume_workflow

    try:
        run = WorkflowRun.load(run_id)
    except (FileNotFoundError, KeyError):
        click.echo(f"No run '{run_id}'.", err=True)
        sys.exit(1)

    if run.status != "waiting":
        click.echo(f"Run {run_id} is '{run.status}', not 'waiting'.", err=True)
        sys.exit(1)

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    wf = dispatcher.find_workflow(run.workflow_name)
    if not wf:
        click.echo(f"Workflow '{run.workflow_name}' not found.", err=True)
        sys.exit(1)

    click.echo(f"Resuming {run.workflow_name} for {run.run_key} "
               f"from step {run.suspended_at_step}...")
    success = resume_workflow(run, wf, timeout=timeout)
    if success:
        click.echo("Workflow completed.")
    else:
        click.echo("Workflow failed.", err=True)
        sys.exit(1)


@workflows.command("validate")
@click.argument("path", type=click.Path(exists=True))
def workflow_validate(path):
    """Validate a workflow YAML file.

    Parses the YAML, checks the DAG structure, reports variable scopes used,
    and prints the topological execution order if valid.

    Usage:
        bobi agent eng workflows validate workflows/deploy.yaml
        bobi agent eng workflows validate package/workflows/deploy.yaml
    """
    import re
    from .workflow.schema import load_workflow
    try:
        wf = load_workflow(Path(path))
        step_names = [s.name for s in wf.steps]
        click.echo(f"Valid: {wf.name} ({len(wf.steps)} steps)")
        if wf.trigger:
            click.echo(f"Trigger: {wf.trigger.strip()}")
        click.echo(f"Steps: {' -> '.join(step_names)}")

        raw = Path(path).read_text()
        refs = set(re.findall(r'\$\{\{(\w+)\.', raw))
        if refs:
            click.echo(f"Variable scopes: {', '.join(sorted(refs))}")

    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)




main.add_command(workflows)


@main.group()
def roles():
    """Agent roles — list available role prompts."""
    pass


@roles.command("list")
def role_list():
    """List available agent roles.

    Scans the selected Bobi Agent's installed package.

    Usage:
        bobi agent eng roles list
    """
    from .prompts.resolver import discover_roles, format_role_list

    project_path = _detect_project_root()
    roles = discover_roles(project_path)
    click.echo(format_role_list(roles))


main.add_command(roles)


@main.group()
def monitors():
    """Background monitoring tasks — scheduled polling to fill webhook gaps."""
    pass


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "monitor"


@monitors.command("list")
def monitor_list():
    """Show the merged view of monitors across all tiers, with source.

    Usage:
        bobi agent eng monitors list
    """
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    registry = MonitorRegistry.load(project_path=project_path)
    monitors = sorted(registry.all_monitors(), key=lambda m: (m.name, m.project))
    if not monitors:
        click.echo("No monitors found.")
        return

    for m in monitors:
        if m.source == "default":
            tier = "default"
        elif m.source == "user":
            tier = "user"
        else:
            tier = f"project:{Path(m.source).name}"
        status = "active" if m.enabled else "paused"
        scope = Path(m.project).name if m.project else "all projects"
        runner = m.check or "manager"
        suffix = _script_cache_summary(m) if m.check == "script_cache" else ""
        click.echo(f"  {m.name:22s} {tier:16s} {m.interval:>5s}  {status:7s} "
                   f"{scope:16s} {m.event:30s} [{runner}]{suffix}")


def _script_cache_summary(monitor) -> str:
    """A compact `mode + cumulative savings` suffix for a script_cache monitor,
    read from its trusted-state sidecar (#327 observability)."""
    try:
        from .monitors.script_cache_checks import _load_trusted_state
        st = _load_trusted_state(monitor.name)
        if not st:
            return "  (no runs yet)"
        cached = st.get("cached_runs", 0)
        fallback = st.get("fallback_runs", 0)
        spent = st.get("total_agent_cost_usd", 0.0)
        avg = (spent / fallback) if fallback else 0.0
        saved = cached * avg  # cached ticks would each have cost ~one agent run
        return (f"  mode={st.get('last_mode', '?')} cached={cached} "
                f"agent={fallback} spent=${spent:.4f} saved~${saved:.4f}")
    except Exception:
        return ""


@monitors.command("add")
@click.argument("name")
@click.option("--interval", default=None, help="How often to run (e.g. 5m, 15m, 1h). Mutually exclusive with --at.")
@click.option("--at", "at_times", multiple=True, help="Wall-clock time(s) HH:MM (repeatable). Schedules instead of --interval.")
@click.option("--tz", default="", help="IANA timezone for --at (e.g. America/Los_Angeles); defaults to host local.")
@click.option("--days", default="", help="Weekday(s) to gate --at to (e.g. 'sun' or 'mon,wed,fri'). Requires --at.")
@click.option("--notify", is_flag=True, help="Fire the event on every scheduled run (a scheduled nudge, not a condition).")
@click.option("--description", default="", help="What the monitor checks (interpreted by the manager)")
@click.option("--event", default=None, help="Synthetic event type to inject (default monitor/<name>)")
@click.option("--check", default="", help="Native check runner (pr_conflicts, stale_prs)")
@click.option("--url", default=None, help="URL the description references (e.g. deploy health)")
def monitor_add(name, interval, at_times, tz, days, notify, description, event, check, url):
    """Add a monitor to the selected Bobi Agent.

    Usage:
        bobi agent eng monitors add "PR conflict check" --interval 15m \\
            --description "Check open PRs for merge conflicts"
        bobi agent eng monitors add deploy-health --interval 5m \\
            --url https://example.com
        bobi agent eng monitors add weekly-prep-doc \\
            --at 21:00 --days sun --tz America/Los_Angeles --notify \\
            --event monitor/prep.weekly_due \\
            --description "Generate my prep doc for the upcoming week"
    """
    import re as _re

    from .monitors.schema import Monitor, parse_at, parse_days, parse_interval
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    if not project_path:
        click.echo("No Bobi Agent runtime selected.", err=True)
        raise SystemExit(1)

    at_list = list(at_times)
    day_list = [d for d in _re.split(r"[,\s]+", days.strip()) if d]

    if at_list and interval is not None:
        raise click.ClickException("--interval and --at are mutually exclusive")
    if day_list and not at_list:
        raise click.ClickException("--days only applies to --at scheduling (add --at HH:MM)")

    slug = _slugify(name)
    try:
        if at_list:
            parse_at(at_list)
            parse_days(day_list)
        else:
            parse_interval(interval or "15m")
    except ValueError as e:
        raise click.ClickException(str(e))

    extra = {}
    if url:
        extra["url"] = url

    m = Monitor(
        name=slug,
        description=description,
        interval=interval or "15m",
        at=at_list,
        tz=tz,
        days=day_list,
        notify=notify,
        event=event or f"monitor/{slug}",
        check=check,
        extra=extra,
    )

    MonitorRegistry.add_project(m, project_path)
    click.echo(f"Added monitor '{slug}' to {paths.package_dir(project_path) / 'monitors.yaml'}")
    if at_list:
        schedule = f"at={','.join(at_list)}"
        if day_list:
            schedule += f" days={','.join(day_list)}"
        if tz:
            schedule += f" tz={tz}"
    else:
        schedule = f"interval={interval or '15m'}"
    click.echo(f"  {schedule} event={m.event} "
               f"{'notify' if notify else (check or 'manager-interpreted')}")


@monitors.command("pause")
@click.argument("name")
def monitor_pause(name):
    """Disable a monitor (writes enabled: false).

    Usage:
        bobi agent eng monitors pause stale-pr-check
    """
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    if MonitorRegistry.pause(name, project_path):
        where = str(paths.package_dir(project_path) / "monitors.yaml") if project_path else "package/monitors.yaml"
        click.echo(f"Paused monitor '{name}' (enabled: false in {where})")
    else:
        click.echo(f"No monitor named '{name}' found.", err=True)
        raise SystemExit(1)


@monitors.command("remove")
@click.argument("name")
def monitor_remove(name):
    """Remove a monitor from the selected Bobi Agent.

    Built-in defaults can't be deleted — pause them instead.

    Usage:
        bobi agent eng monitors remove deploy-health
    """
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    result = MonitorRegistry.remove(name, project_path)
    if result == "removed":
        click.echo(f"Removed monitor '{name}'.")
    elif result == "default-only":
        click.echo(f"'{name}' is a built-in default and can't be removed. "
                   f"Use `bobi agent <agent> monitors pause {name}` to disable it.", err=True)
        raise SystemExit(1)
    else:
        click.echo(f"No monitor named '{name}' found in a writable tier.", err=True)
        raise SystemExit(1)


def _find_monitor(name: str, project_path):
    """Resolve a monitor by name from the effective registry, or None."""
    from .monitors.registry import MonitorRegistry
    registry = MonitorRegistry.load(project_path=project_path)
    for m in registry.effective_monitors():
        if m.name == name:
            return m
    return None


@monitors.command("recache")
@click.argument("name")
def monitor_recache(name):
    """Invalidate a script_cache monitor's cached script (forces regeneration).

    Usage:
        bobi agent eng monitors recache unread-emails
    """
    from .monitors.script_cache_checks import recache

    project_path = _detect_project_root()
    m = _find_monitor(name, project_path)
    if m is None:
        click.echo(f"No monitor named '{name}' found.", err=True)
        raise SystemExit(1)
    if m.check != "script_cache":
        click.echo(f"'{name}' is not a script_cache monitor (check={m.check}).", err=True)
        raise SystemExit(1)
    recache(m)
    click.echo(f"Invalidated cached script for '{name}' — next tick regenerates.")


@monitors.command("approve-script")
@click.argument("name")
def monitor_approve_script(name):
    """Promote a script_cache monitor's pending script to active (review mode).

    Usage:
        bobi agent eng monitors approve-script unread-emails
    """
    from .monitors.script_cache_checks import approve_pending

    project_path = _detect_project_root()
    m = _find_monitor(name, project_path)
    if m is None:
        click.echo(f"No monitor named '{name}' found.", err=True)
        raise SystemExit(1)
    if m.check != "script_cache":
        click.echo(f"'{name}' is not a script_cache monitor (check={m.check}).", err=True)
        raise SystemExit(1)
    if approve_pending(m):
        click.echo(f"Approved + pinned the pending script for '{name}'.")
    else:
        click.echo(f"No valid pending script to approve for '{name}'.", err=True)
        raise SystemExit(1)


main.add_command(monitors)


# ---------------------------------------------------------------------------
# event-server group
# ---------------------------------------------------------------------------


@main.group("event-server")
def event_server_cmd():
    """Manage the local event server daemon."""
    pass


@event_server_cmd.command("start")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground")
@click.option("--port", default=None, type=int, help="Override webhook port")
def event_server_start(foreground, port):
    """Start the local event server."""
    project_path = _detect_project_root()
    es_port = _selected_local_event_server_port(project_path, port)

    from bobi.events.server import ensure_running
    result = ensure_running(es_port, project_path=project_path)
    if result == "skipped":
        click.echo("Remote event_server_url configured — local server not needed.", err=True)
        return

    if foreground:
        click.echo(f"Event server running on port {es_port} (foreground)")
        try:
            import time
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        return

    click.echo(f"Event server running on port {es_port}")
    click.echo(f"  GitHub:  http://localhost:{es_port}/webhooks/github")
    click.echo(f"  Linear:  http://localhost:{es_port}/webhooks/linear")
    click.echo(f"  Slack:   http://localhost:{es_port}/webhooks/slack")


@event_server_cmd.command("stop")
def event_server_stop():
    """Stop the local event server."""
    import signal
    project_path = _detect_project_root()
    if not project_path:
        click.echo("Not inside a bobi project.", err=True)
        raise SystemExit(1)
    pid_file = _project_state_dir(project_path) / "event-server.pid"
    port_file = _event_server_port_file(project_path)
    if not pid_file.exists():
        click.echo("Event server is not running")
        port_file.unlink(missing_ok=True)
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Event server stopped (pid {pid})")
    except ProcessLookupError:
        click.echo("Event server was not running (stale PID file)")
    pid_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)


@event_server_cmd.command("restart")
@click.option("--port", default=None, type=int, help="Override webhook port")
@click.pass_context
def event_server_restart(ctx, port):
    """Restart the local event server."""
    ctx.invoke(event_server_stop)
    import time as _time
    _time.sleep(1)
    ctx.invoke(event_server_start, foreground=False, port=port)


@event_server_cmd.command("status")
def event_server_status():
    """Show event server status."""
    from bobi.events.server import health
    project_path = _detect_project_root()
    try:
        from .config import Config
        configured = Config.load(project_path).event_server_url
    except Exception:
        configured = ""
    if configured and _parse_local_event_server_port(configured) is None:
        click.echo(f"Event server: remote ({configured})")
        return

    es_port = _selected_local_event_server_port(project_path)
    data = health(f"http://localhost:{es_port}")
    if data:
        click.echo(f"Event server: running on port {es_port}")
        click.echo(f"  Mode: {data.get('mode', 'unknown')}")
        click.echo(f"  Deployments: {data.get('deployments', 0)}")
    else:
        click.echo(f"Event server: not running (port {es_port})")


main.add_command(event_server_cmd)


@subagents.command("launch")
@click.option("--workflow", "-w", required=True, help="Workflow to run (e.g. issue-lifecycle, adhoc)")
@click.option("--role", required=True, help="Agent role (see 'bobi agent <name> roles list')")
@click.option("--id", "run_key", default=None, help="Explicit run key for correlation (e.g. issue number)")
@click.option("--task", default=None, help="Task description / context for the agent")
@click.option("--timeout", default=3600, type=int, help="Timeout in seconds")
@click.option("--wait", is_flag=True, help="Block until the agent completes")
@click.option("--post-event", "post_event", default=None,
              help="Post this event type on completion (for --wait checks)")
@click.option("--requested-by", "requested_by", default=None,
              help='JSON identity of requester, e.g. \'{"from":"Alice","channel":"C1"}\'')
@click.option("--non-interactive", "non_interactive", is_flag=True,
              help="Run without manager — agent makes all decisions autonomously")
@click.option("--persistent", is_flag=True,
              help="Keep the agent alive after initial task, accepting inbox messages")
@click.option("--subscribe", multiple=True,
              help="Subscribe to event topics (e.g. moda-labs/bobi-agent, slack:T123)")
def subagents_launch(workflow, role, run_key, task, timeout, wait, post_event, requested_by, non_interactive, persistent, subscribe):
    """Launch a sub-agent with a workflow and role.

    Every sub-agent runs a workflow with a role. Use 'adhoc' for open-ended tasks.
    Use 'bobi agent <name> roles list' to see available roles.

    Examples:
        bobi agent eng subagents launch -w issue-lifecycle --role engineer --id 42 --task "Fix moda-labs/bobi-agent#42"
        bobi agent eng subagents launch -w adhoc --role engineer --task "Why is CI failing?"
    """
    if subscribe:
        persistent = True
    _dispatch_agent(task=task, workflow=workflow, role=role, run_key=run_key,
                    timeout=timeout, wait=wait, post_event=post_event,
                    requested_by=requested_by,
                    interactive=not non_interactive,
                    persistent=persistent,
                    subscribe=list(subscribe))


def _dispatch_agent(*, task, workflow, role, run_key=None, timeout, wait, post_event,
                    requested_by, interactive=True, persistent=False, subscribe=None):
    """Dispatch logic for the agent command."""
    if not workflow:
        click.echo("--workflow is required. Use 'adhoc' for open-ended tasks.", err=True)
        raise SystemExit(1)

    if not task:
        task = f"Run workflow {workflow}"

    # Raises a clean UsageError when run outside an installation.
    project_path = _detect_project_root()
    cwd = str(project_path)

    if wait:
        _run_check(cwd=cwd, task=task, timeout=timeout, post_event=post_event)
        return

    # --- Validate role ---
    from .prompts.resolver import validate_role, discover_roles
    if not validate_role(role, Path(cwd)):
        available = discover_roles(Path(cwd))
        names = ", ".join(r["name"] for r in available) if available else "(none)"
        click.echo(f"Unknown role '{role}'. Available: {names}", err=True)
        raise SystemExit(1)

    requester: dict = {}
    if requested_by:
        try:
            parsed = json.loads(requested_by)
            if isinstance(parsed, dict):
                requester = parsed
            else:
                click.echo("--requested-by must be a JSON object", err=True)
                raise SystemExit(1)
        except json.JSONDecodeError:
            click.echo("--requested-by must be valid JSON", err=True)
            raise SystemExit(1)

    from .subagent import launch_agent
    session_name = launch_agent(
        task=task, cwd=cwd, workflow_name=workflow,
        timeout=timeout, requested_by=requester,
        interactive=interactive,
        role=role,
        persistent=persistent,
        subscribe=subscribe or [],
        run_key=run_key,
    )
    click.echo(f"Agent started: {session_name}")



def _run_check(cwd: str, task: str, timeout: int, post_event: str | None) -> None:
    """Run a non-interactive check, print its verdict, optionally post an event.

    Used by `bobi spawn --non-interactive` and by the monitor scheduler,
    which launches this as a short-lived out-of-band process so the manager's
    context stays clean — the manager only ever sees the resulting event.
    """
    from .subagent import run_check_blocking

    # Cap the check's runtime well below an engineer phase — checks are quick.
    from .subagent import CHECK_TIMEOUT
    check_timeout = min(timeout, CHECK_TIMEOUT) if timeout else CHECK_TIMEOUT

    result = run_check_blocking(description=task, cwd=cwd, timeout=check_timeout)

    verdict = {
        "success": result.success,
        "finding": result.finding,
        "summary": result.summary,
        "details": result.details,
    }
    click.echo(json.dumps(verdict))

    if not result.success:
        click.echo(f"Check failed: {result.error}", err=True)
        raise SystemExit(1)

    if post_event and result.finding:
        data = {"summary": result.summary, "text": result.summary, **result.details}
        if _post_event(post_event, data):
            click.echo(f"Posted event: {post_event}")
        else:
            click.echo(f"Could not post event: {post_event}", err=True)
            raise SystemExit(1)


def _post_event(event_type: str, data: dict) -> bool:
    """Post a synthetic event to the event server (see events/publish.py)."""
    from bobi.events.publish import post_event
    return post_event(event_type, data, project_path=_detect_project_root())


@agents.command("update")
@click.argument("name", default=None, required=False)
def agents_update(name):
    """Update agent teams from the remote registry.

    Usage:
        bobi agents update eng-team         # update one pack to latest
        bobi agents update eng-team@1.1.0   # pin to an immutable version
        bobi agents update                  # update all cached packs
    """
    from bobi.registry import (fetch, list_cached, check_update,
                                    split_team_ref, _read_local_version)

    project_path = paths.home_dir()

    if name:
        pkg_name, version = split_team_ref(name)  # D-6: split on the last `@`
        try:
            if version:
                # A pin targets an immutable asset — fetch directly (idempotent),
                # no latest-vs-local short-circuit.
                fetch(project_path, pkg_name, version=version)
                new_v = _read_local_version(project_path, pkg_name) or version
                click.echo(f"Pinned {pkg_name} to v{new_v}")
                return
            local_v, remote_v = check_update(project_path, pkg_name)
            if local_v and remote_v and remote_v == local_v:
                click.echo(f"{pkg_name} v{local_v} is already up to date.")
                return
            path = fetch(project_path, pkg_name)
            new_v = _read_local_version(project_path, pkg_name) or "unknown"
            if local_v:
                click.echo(f"Updated {pkg_name}: v{local_v} → v{new_v}")
            else:
                click.echo(f"Installed {pkg_name} v{new_v} → {path}")
        except Exception as e:
            click.echo(f"Failed: {e}", err=True)
            raise SystemExit(1)
    else:
        cached = list_cached(project_path)
        if not cached:
            click.echo("No cached agent teams to update.")
            return
        for pack in cached:
            try:
                local_v, remote_v = check_update(project_path, pack["name"])
                if local_v and remote_v and remote_v == local_v:
                    click.echo(f"  {pack['name']} v{local_v} — up to date")
                elif remote_v:
                    fetch(project_path, pack["name"])
                    click.echo(f"  {pack['name']} v{local_v} → v{remote_v}")
                else:
                    click.echo(f"  {pack['name']} v{local_v} — could not check remote")
            except Exception as e:
                click.echo(f"  {pack['name']} — failed: {e}", err=True)


@agents.command("add-registry")
@click.argument("repo")
def agents_add_registry(repo):
    """Add a registry to fetch agent teams from.

    A registry is a GitHub repo containing an agents/ directory
    with agent teams and a registry.yaml index.

    Usage:
        bobi agents add-registry myorg/my-agents
    """
    import yaml as _yaml

    config_path = paths.ensure_global_config()
    raw = _yaml.safe_load(config_path.read_text()) or {}
    registries = raw.get("registries", [])

    if repo in registries:
        click.echo(f"Registry '{repo}' is already configured.")
        return

    registries.append(repo)
    raw["registries"] = registries
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_yaml.dump(raw, default_flow_style=False))
    click.echo(f"Added registry: {repo}")


@agents.command("remove-registry")
@click.argument("repo")
def agents_remove_registry(repo):
    """Remove a registry.

    Usage:
        bobi agents remove-registry myorg/my-agents
    """
    import yaml as _yaml

    config_path = paths.ensure_global_config()
    raw = _yaml.safe_load(config_path.read_text()) or {}
    registries = raw.get("registries", [])

    if repo not in registries:
        click.echo(f"Registry '{repo}' is not configured.", err=True)
        raise SystemExit(1)

    registries.remove(repo)
    raw["registries"] = registries
    config_path.write_text(_yaml.dump(raw, default_flow_style=False))
    click.echo(f"Removed registry: {repo}")


@agents.command("browse")
def agents_browse():
    """Browse available agent teams from the remote registry.

    Shows all packs available for install, along with their versions
    and whether they're already cached locally.

    Usage:
        bobi agents browse
    """
    from bobi.registry import list_remote, list_cached, DEFAULT_REPO

    project_path = paths.home_dir()
    remote = list_remote(project_path)
    if not remote:
        click.echo("Could not fetch remote registry.", err=True)
        raise SystemExit(1)

    cached_packs = list_cached(project_path) if project_path else []
    cached = {p["name"]: p["version"] for p in cached_packs}

    click.echo("Available agent teams:\n")
    for pack in remote:
        name = pack["name"]
        version = pack.get("version", "?")
        desc = pack.get("description", "")
        registry = pack.get("registry", DEFAULT_REPO)
        local_v = cached.get(name)
        if local_v:
            if local_v == version:
                status = "installed"
            else:
                status = f"v{local_v} → v{version} available"
        else:
            status = "not installed"
        click.echo(f"  {name:20s} v{version:8s} [{status}]")
        if desc:
            click.echo(f"  {'':20s} {desc}")
        if registry != DEFAULT_REPO:
            click.echo(f"  {'':20s} registry: {registry}")
        click.echo()

    click.echo("Install with: bobi agents update <name>")


# ---------------------------------------------------------------------------
# kb group
# ---------------------------------------------------------------------------

@main.group()
def kb():
    """Knowledge base — create, populate, and search named KBs."""
    pass


@kb.command("create")
@click.argument("name")
def kb_create(name):
    """Create a new knowledge base.

    Usage:
        bobi agent <name> kb create docs
    """
    from bobi.kb.store import KBStore
    _ensure_root_bound()
    try:
        store = KBStore.create(name)
        click.echo(f"Created KB '{name}'")
    except FileExistsError:
        click.echo(f"KB '{name}' already exists.", err=True)
        raise SystemExit(1)


@kb.command("add")
@click.argument("name")
@click.option("--file", "-f", "file_path", type=click.Path(exists=True),
              help="Path to file to add")
@click.option("--text", "-t", "text", help="Inline text to add")
def kb_add(name, file_path, text):
    """Add content to a knowledge base.

    Usage:
        bobi agent <name> kb add docs --file README.md
        bobi agent <name> kb add docs --text "Important fact"
    """
    from bobi.kb.store import KBStore
    from bobi.kb.embedder import embed
    _ensure_root_bound()

    try:
        store = KBStore(name)
    except FileNotFoundError:
        click.echo(f"KB '{name}' does not exist. Create it first with the named kb create command.", err=True)
        raise SystemExit(1)

    if file_path:
        ids = store.add_file(Path(file_path), embed_fn=embed)
        if not ids:
            click.echo(f"File already indexed (unchanged)")
        else:
            click.echo(f"Added {len(ids)} chunks from {file_path}")
    elif text:
        ids = store.add_text(text, embed_fn=embed)
        click.echo(f"Added {len(ids)} chunks")
    else:
        click.echo("Provide --file or --text", err=True)
        raise SystemExit(1)


@kb.command("search")
@click.argument("name")
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--mode", type=click.Choice(["hybrid", "fts", "vector"]),
              default="hybrid", help="Search mode")
def kb_search(name, query, limit, mode):
    """Search a knowledge base.

    Usage:
        bobi agent <name> kb search docs "authentication flow"
        bobi agent <name> kb search docs "login bug" --limit 5
        bobi agent <name> kb search docs "exact phrase" --mode fts
    """
    from bobi.kb.store import KBStore
    from bobi.kb.embedder import embed
    _ensure_root_bound()

    try:
        store = KBStore(name)
    except FileNotFoundError:
        click.echo(f"KB '{name}' does not exist.", err=True)
        raise SystemExit(1)

    embed_fn = embed if mode in ("hybrid", "vector") else None
    results = store.search(query, limit=limit, embed_fn=embed_fn)

    if not results:
        click.echo("No results.")
        return

    for i, r in enumerate(results, 1):
        source = r.get("source", "")
        score = r.get("score", 0)
        content = r["content"][:200].replace("\n", " ")
        click.echo(f"  {i}. [{score:.3f}] {source}")
        click.echo(f"     {content}")
        click.echo()


@kb.command("list")
def kb_list():
    """List all knowledge bases.

    Usage:
        bobi agent <name> kb list
    """
    from bobi.kb.store import KBStore
    _ensure_root_bound()

    kbs = KBStore.list_kbs()
    if not kbs:
        click.echo("No knowledge bases. Create one with the named kb create command.")
        return
    for k in kbs:
        click.echo(f"  {k['name']:20s} {k['entry_count']} entries  {k['created_at'][:19]}")


@kb.command("info")
@click.argument("name")
def kb_info(name):
    """Show knowledge base statistics.

    Usage:
        bobi agent <name> kb info docs
    """
    from bobi.kb.store import KBStore
    _ensure_root_bound()

    try:
        store = KBStore(name)
    except FileNotFoundError:
        click.echo(f"KB '{name}' does not exist.", err=True)
        raise SystemExit(1)

    info = store.info()
    click.echo(f"  Name:       {info['name']}")
    click.echo(f"  Entries:    {info['entry_count']}")
    click.echo(f"  Sources:    {info['source_count']}")
    click.echo(f"  Model:      {info['embedding_model']}")
    click.echo(f"  Created:    {info['created_at']}")
    if info.get("sources"):
        click.echo(f"  Files:")
        for s in info["sources"]:
            click.echo(f"    {s['source']}: {s['count']} chunks")


@kb.command("remove")
@click.argument("name")
@click.confirmation_option(prompt="Delete this knowledge base?")
def kb_remove(name):
    """Delete a knowledge base.

    Usage:
        bobi agent <name> kb remove docs
    """
    from bobi.kb.store import KBStore
    _ensure_root_bound()

    try:
        KBStore.remove(name)
        click.echo(f"Removed KB '{name}'")
    except FileNotFoundError:
        click.echo(f"KB '{name}' does not exist.", err=True)
        raise SystemExit(1)


main.add_command(kb)


# ---------------------------------------------------------------------------
# costs command
# ---------------------------------------------------------------------------


@main.command()
@click.option("--by", "group_by", default="provider",
              type=click.Choice(["provider", "model", "session", "role"]),
              help="Group costs by dimension")
def costs(group_by):
    """Show cost attribution across sessions, grouped by provider/model/role.

    Aggregates total_cost_usd and model_usage from all session state files.

    Usage:
        bobi agent <name> costs
        bobi agent <name> costs --by model
        bobi agent <name> costs --by role
        bobi agent <name> costs --by session
    """
    from .costs import rollup_costs, format_costs

    project_path = _detect_project_root()
    sessions_dir = paths.sessions_dir(project_path)
    summary = rollup_costs(sessions_dir, group_by=group_by)

    if summary.sessions_counted == 0:
        click.echo("No cost data found. Costs are recorded as sessions run.")
        return

    click.echo(format_costs(summary, group_by=group_by))


for _cmd_name in [
    "start", "stop", "restart", "status", "ui", "message", "ask", "compact",
    "events", "costs", "doctor", "login-bootstrap",
]:
    if _cmd_name in main.commands:
        agent.add_command(main.commands[_cmd_name])

for _group_name in ["transcript", "workflows", "roles", "monitors", "kb", "event-server"]:
    if _group_name in main.commands:
        agent.add_command(main.commands[_group_name])

for _cmd_name in ["install"]:
    if _cmd_name in main.commands:
        agents.add_command(main.commands[_cmd_name])

for _old_top_level in [
    "start", "stop", "restart", "status", "ui", "message", "ask", "compact",
    "events", "costs", "doctor", "transcript", "workflows", "roles", "monitors", "kb",
    "event-server", "login-bootstrap", "install",
]:
    main.commands.pop(_old_top_level, None)


if __name__ == "__main__":
    main()
