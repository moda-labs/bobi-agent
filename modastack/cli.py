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

from .__version__ import __version__

REPO_ROOT = Path(__file__).parent.parent


def _print_startup_info(project_path: Path, pid: int, log_file: Path):
    """Print a startup summary with environment info."""
    from .config import Config

    lines = []
    lines.append(f"modastack v{__version__}")
    lines.append(f"  project     {project_path.name} ({project_path})")
    lines.append(f"  pid         {pid}")

    try:
        cfg = Config.load(project_path)
        if cfg.event_server_url:
            label = "remote" if not cfg.event_server_url.startswith("http://localhost") else "local"
            lines.append(f"  event server  {cfg.event_server_url} ({label})")
        else:
            lines.append(f"  event server  not configured")
    except Exception:
        pass

    wf_dir = project_path / ".modastack" / "workflows"
    if wf_dir.exists():
        wf_names = sorted(p.stem for p in wf_dir.glob("*.yaml"))
        if wf_names:
            lines.append(f"  workflows   {', '.join(wf_names)}")

    mon_file = project_path / ".modastack" / "monitors.yaml"
    if mon_file.exists():
        try:
            import yaml
            raw = yaml.safe_load(mon_file.read_text()) or {}
            monitors = raw.get("monitors", [])
            active = [m["name"] for m in monitors if isinstance(m, dict) and m.get("enabled", True)]
            if active:
                lines.append(f"  monitors    {', '.join(active)}")
        except Exception:
            pass

    lines.append(f"  logs        {log_file}")

    click.echo("\n".join(lines))

def _detect_project_root(cwd: Path | None = None) -> Path | None:
    """Walk up from cwd to find a project with .modastack/config.yaml."""
    path = (cwd or Path.cwd()).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".modastack" / "config.yaml").exists():
            return candidate
    return None


def _project_state_dir(project_path: Path) -> Path:
    """Runtime state directory for a project's manager."""
    d = project_path / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d




@click.group()
@click.version_option(version=__version__, prog_name="modastack")
def main():
    """Modastack — AI engineering manager + engineer team."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler()],
    )
    project = _detect_project_root()
    if project:
        from modastack.sdk import set_project_root
        set_project_root(project)
        state = _project_state_dir(project)
        logging.getLogger().addHandler(logging.FileHandler(state / "manager.log"))


def _has_systemd_service() -> bool:
    """Check if modastack is managed by a systemd user service."""
    svc = Path.home() / ".config" / "systemd" / "user" / "modastack.service"
    if not svc.exists():
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "modastack"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _systemctl(action: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", action, "modastack"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        click.echo(f"systemctl {action} failed: {result.stderr.strip()}", err=True)
        return False
    return True


def _generate_default_agent_config(project_path: Path) -> dict:
    """Generate a default agent config from project settings."""
    from modastack.events.subscriptions import build_subscriptions
    subs = build_subscriptions(project_path)
    return {
        "role": "manager",
        "persistent": True,
        "subscribe": subs,
        "monitors": True,
    }


def _load_agent_config(project_path: Path, config_path: str | None = None) -> dict | None:
    """Load agent config from .modastack/agent.yaml or explicit path."""
    import yaml
    if config_path:
        p = Path(config_path)
    else:
        p = project_path / ".modastack" / "agent.yaml"
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text()) or {}


def _run_from_agent_config(project_path: Path, config: dict) -> None:
    """Start an agent from a config dict — the new modastack start path."""
    import atexit
    import signal
    import threading

    from modastack.sdk import set_project_root
    set_project_root(project_path)

    agent_name = config.get("agent")
    role = config.get("role", "manager")
    subscribe = config.get("subscribe", [])
    persistent = config.get("persistent", True)
    monitors_enabled = config.get("monitors", False)

    state_dir = project_path / ".modastack" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    pid_str = str(os.getpid())
    (state_dir / "manager.pid").write_text(pid_str)

    def _cleanup():
        pid_file = state_dir / "manager.pid"
        try:
            if pid_file.exists() and pid_file.read_text().strip() == pid_str:
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass
    atexit.register(_cleanup)

    log = logging.getLogger(__name__)

    def _handle_term(signum, frame):
        log.info("Received SIGTERM — shutting down")
        _cleanup()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_term)

    log.info(f"Modastack starting for {project_path.name} (role={role})")

    if subscribe:
        from modastack.subagent import _start_event_subscription
        _start_event_subscription(f"moda-{role}-{project_path.name}", subscribe, project_path)

    if monitors_enabled:
        from modastack.monitors.scheduler import MonitorScheduler
        monitor_scheduler = MonitorScheduler(agent_name=agent_name)
        monitor_scheduler.start()
        log.info("Monitor scheduler started")

    from modastack.prompts.resolver import build_startup_prompt
    from modastack.subagent import spawn_adhoc

    task = config.get("task") or build_startup_prompt(role, project_path, agent_name=agent_name)

    log.info(f"Modastack running for {project_path.name}")
    spawn_adhoc(
        cwd=str(project_path),
        task=task,
        name=f"moda-{role}-{project_path.name}",
        persistent=persistent,
        role=role,
    )


@main.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in the foreground (default: daemonize)")
@click.option("--fresh", is_flag=True, help="Wipe manager session and start clean")
@click.option("--non-interactive", is_flag=True, envvar="CI", help="Skip interactive setup prompts")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True),
              help="Agent config file (default: .modastack/agent.yaml)")
def start(foreground, fresh, non_interactive, config_path):
    """Start modastack for the project in the current directory.

    Reads .modastack/agent.yaml (or --config) to determine which role,
    subscriptions, and capabilities to start with. If no agent.yaml exists,
    a default config is generated from project settings.

    Usage:
        cd myrepo && modastack start              # daemonize
        cd myrepo && modastack start --foreground  # run in foreground
        cd myrepo && modastack start --fresh       # fresh manager session
        cd myrepo && modastack start --config path/to/agent.yaml
    """
    _ensure_config(non_interactive)

    project_path = _detect_project_root()
    if not project_path:
        click.echo("Not inside a modastack project (no .modastack/config.yaml found).", err=True)
        raise SystemExit(1)

    if fresh:
        _clear_manager_session(project_path)

    if not foreground and _has_systemd_service():
        click.echo("Starting via systemd...")
        if _systemctl("start"):
            result = subprocess.run(
                ["systemctl", "--user", "show", "modastack", "--property=MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            pid = int(result.stdout.strip() or "0")
            _print_startup_info(project_path, pid, _project_state_dir(project_path) / "manager.log")
        return

    state_dir = _project_state_dir(project_path)
    pid_path = state_dir / "manager.pid"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Modastack already running for {project_path.name} (pid {pid}). Use `modastack restart`.")
            return
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)

    agent_config = _load_agent_config(project_path, config_path)
    if not agent_config:
        agent_config = _generate_default_agent_config(project_path)

    if foreground:
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers
                         if isinstance(h, logging.FileHandler)]

        _run_from_agent_config(project_path, agent_config)
    else:
        log_file = state_dir / "manager.log"
        env = os.environ.copy()
        venv_bin = str(Path(sys.executable).parent)
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{venv_bin}:{local_bin}:{env.get('PATH', '')}"
        cmd = [sys.executable, "-m", "modastack.cli", "start", "--foreground"]
        if fresh:
            cmd.append("--fresh")
        if config_path:
            cmd.extend(["--config", config_path])
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf, stderr=lf,
                cwd=str(project_path),
                env=env,
                start_new_session=True,
            )
        _print_startup_info(project_path, proc.pid, log_file)


def _ensure_config(non_interactive: bool) -> None:
    """Auto-register with event server on first run if needed."""
    project_path = _detect_project_root()
    if not project_path:
        return

    from .config import Config, load_deployment_state, save_deployment_state
    cfg = Config.load(project_path)
    state = load_deployment_state(project_path)

    if cfg.event_server_url and not state.get("api_key"):
        click.echo("  Registering with event server...")
        deployment_id, api_key = _register_event_server(
            cfg.event_server_url, project_path, cfg,
        )
        if deployment_id:
            save_deployment_state(project_path, deployment_id, api_key)
            click.echo(f"  Registered: {deployment_id[:8]}...")
        else:
            click.echo("  Could not register (will retry on next start)")


def _register_event_server(url: str, project_path: Path, rc: "ProjectConfig") -> tuple[str, str]:
    """Register with the event server and return (deployment_id, api_key)."""
    import urllib.request
    import urllib.error
    from modastack.events.subscriptions import build_subscriptions
    try:
        subs = build_subscriptions(project_path)
        payload = json.dumps({
            "name": project_path.name,
            "subscriptions": subs,
        }).encode()
        req = urllib.request.Request(
            f"{url}/deployments",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("deployment_id", ""), data.get("api_key", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
        logging.getLogger(__name__).warning(f"Event server registration failed: {e}")
        return "", ""


def _clear_manager_session(project_path: Path) -> None:
    """Wipe saved session ID so the manager starts a fresh conversation."""
    from modastack.sdk import save_session_id
    session_name = f"moda-mgr-{project_path.name}"
    save_session_id(session_name, "")
    click.echo("Cleared manager session — starting fresh.")


def _find_pid_path() -> Path | None:
    """Find the PID file for the current project's manager."""
    project_path = _detect_project_root()
    if project_path:
        p = _project_state_dir(project_path) / "manager.pid"
        if p.exists():
            return p
    return None


@main.command()
@click.option("--force", is_flag=True, help="Send SIGKILL if SIGTERM doesn't work")
def stop(force):
    """Stop the modastack instance for the current project.

    Usage:
        modastack stop
        modastack stop --force
    """
    if _has_systemd_service() and not force:
        click.echo("Stopping via systemd...")
        _systemctl("stop")
        return

    import signal

    pid_path = _find_pid_path()
    if not pid_path:
        click.echo("No PID file found — modastack is not running.")
        return

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
    click.echo(f"Stopping modastack (pid {pid})...")
    os.kill(pid, sig)

    import time
    for _ in range(30):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            click.echo("Stopped.")
            return

    if not force:
        click.echo("Process didn't exit — try: modastack stop --force")
    else:
        pid_path.unlink(missing_ok=True)
        click.echo("Killed.")


@main.command()
@click.option("--fresh", is_flag=True, help="Wipe manager session and start clean")
def restart(fresh):
    """Stop and restart modastack.

    Usage:
        modastack restart
        modastack restart --fresh   # fresh manager session
    """
    if _has_systemd_service():
        if fresh:
            project_path = _detect_project_root()
            if project_path:
                _clear_manager_session(project_path)
        click.echo("Restarting via systemd...")
        _systemctl("restart")
        result = subprocess.run(
            ["systemctl", "--user", "show", "modastack", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        pid = result.stdout.strip()
        project_path = _detect_project_root()
        log_path = _project_state_dir(project_path) / "manager.log" if project_path else "stderr"
        click.echo(f"Modastack restarted (pid {pid}). Logs: {log_path}")
        return

    ctx = click.get_current_context()
    ctx.invoke(stop)
    ctx.invoke(start, fresh=fresh)


def _resolve_address(to: str | None) -> str | None:
    """Resolve a friendly address to a session name.

    'manager' or None → finds the manager session by role.
    Anything else → used as-is (exact session name).
    """
    from modastack.sdk import get_registry, set_project_root

    project_path = _detect_project_root()
    if project_path:
        set_project_root(project_path)

    registry = get_registry()
    if to is None or to == "manager":
        managers = registry.get_by_role("manager")
        active = [m for m in managers if m.status in ("idle", "running", "starting")]
        if active:
            return active[0].name
        if managers:
            return managers[0].name
        return None
    return to


@main.command()
@click.argument("text", required=True)
@click.option("--to", default=None, help="Target session (default: manager)")
@click.option("--wait", is_flag=True, help="Block until the session responds")
@click.option("--timeout", default=300, type=int, help="Timeout in seconds (with --wait)")
def message(text, to, wait, timeout):
    """Send a message to any session via its inbox.

    Usage:
        modastack message "what are you working on?"
        modastack message --to eng-42-implement "try a different approach"
        modastack message --to manager "status?" --wait
    """
    from modastack.inbox import deliver

    address = _resolve_address(to)
    if not address:
        target = to or "manager"
        click.echo(f"No active session found for '{target}'.", err=True)
        raise SystemExit(1)

    ok, response = deliver(address, text, sender="cli", wait=wait, timeout=timeout)
    if ok:
        if wait and response:
            click.echo(response)
        else:
            click.echo(f"Sent to {address}")
    else:
        click.echo(f"Failed: {response}", err=True)
        raise SystemExit(1)


@main.command(hidden=True)
@click.argument("question", required=True)
@click.option("--timeout", default=300, type=int, help="Timeout in seconds")
@click.option("--source", default="engineer", help="Source identifier")
def ask(question, timeout, source):
    """Ask the manager a question (alias for: message --wait)."""
    from modastack.inbox import deliver

    address = _resolve_address("manager")
    if not address:
        click.echo("No active manager session found.", err=True)
        raise SystemExit(1)

    ok, response = deliver(address, question, sender=source, wait=True, timeout=timeout)
    if ok:
        click.echo(response)
    else:
        click.echo(f"Failed: {response}", err=True)
        raise SystemExit(1)


@main.command("slack-reply")
@click.argument("text")
@click.option("--workspace", "-w", required=True, help="Slack workspace ID (e.g. T0952RZRZ0X)")
@click.option("--channel", "-c", required=True, help="Slack channel ID (e.g. D0B51JP1N4C)")
@click.option("--thread", "-t", default="", help="Thread timestamp to reply in")
def slack_reply(text, workspace, channel, thread):
    """Post a message to Slack. Used by the manager to reply to Slack events.

    Usage:
        modastack slack-reply -w T0952RZRZ0X -c D0B51JP1N4C "Hello"
        modastack slack-reply -w T0952RZRZ0X -c C123 -t 1780165787.159589 "Thread reply"
    """
    import re
    import urllib.error
    import urllib.request

    token = ""
    project_path = _detect_project_root()
    if project_path:
        from .config import Config
        cfg = Config.load(project_path)
        token = cfg.slack_bot_token
    if not token:
        click.echo(f"No bot token configured (set slack.bot_token in ~/.modastack/config.yaml)", err=True)
        sys.exit(1)

    # The manager invokes this command through a shell, where newlines in the
    # message arrive as literal "\n" escape sequences rather than real
    # newlines. Convert them back so Slack renders proper line breaks instead
    # of showing the literal characters.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")

    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    if len(text) > 3000:
        text = text[:3000] + '\n_(truncated)_'

    payload: dict = {"channel": channel, "text": text}
    if thread:
        payload["thread_ts"] = thread

    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            click.echo(f"Sent to {channel}")
        else:
            click.echo(f"Slack error: {result.get('error', 'unknown')}", err=True)
            sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        click.echo(f"Failed: {e}", err=True)
        sys.exit(1)


@main.group()
def transcript():
    """Session transcripts — view, search, and index conversation history."""
    pass


@transcript.command("show")
@click.argument("session", default="manager")
@click.option("-n", "--lines", default=30, help="Number of recent messages to show")
@click.option("-f", "--follow", is_flag=True, help="Follow mode — stream new entries")
def transcript_show(session, lines, follow):
    """Show the transcript for a session.

    Usage:
        modastack transcript show manager        # manager transcript
        modastack transcript show eng-70         # engineer transcript
        modastack transcript show manager -n 50  # last 50 messages
        modastack transcript show manager -f     # follow mode
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
    from modastack.sdk import SessionRegistry, get_registry

    if session == "manager":
        project = _detect_project_root()
        session = f"moda-mgr-{project.name}" if project else "moda-manager"

    # Primary: session dir log
    session_log = SessionRegistry.log_path(session)
    if session_log.exists():
        return session_log

    # Fallback: Claude Code transcript via session ID
    from modastack.sdk import _sessions_dir
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
    recent_names = [d.name for d in recent_dirs[:10] if not d.name.startswith("moda-mgr")]
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
    from modastack.sdk import load_session_id, get_registry

    project_path = _detect_project_root()
    running = False
    pid_path = _find_pid_path()
    if pid_path and pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    if not project_path:
        click.echo("Not in a modastack-configured project. Run `modastack start` to set one up.")
        raise SystemExit(1)

    mgr_name = f"moda-mgr-{project_path.name}"
    session_id = load_session_id(mgr_name) or ""
    session_short = session_id[:8] if session_id else ""

    if running:
        mgr_label = f"running (session {session_short})" if session_short else "running"
    else:
        mgr_label = "stopped"
    click.echo(f"  Manager: {mgr_label}")

    registry = get_registry()
    active = [e for e in registry.list_active() if e.role == "engineer"]
    if not active:
        click.echo("  Engineers: none active")
        return

    click.echo(f"  Engineers: {len(active)} active")
    for e in active:
        click.echo(f"    {e.issue_id}/{e.phase} — {e.status}")


@main.command()
@click.option("--browser", is_flag=True, default=False,
              help="Also run /browse + Chromium sandbox checks")
@click.option("--fix", is_flag=True, help="Offer to apply the Chromium sandbox fix (with --browser)")
def doctor(browser, fix):
    """System health check — verify manager, event server, dashboard, repos, workflows.

    Runs a suite of checks and prints a pass/fail line for each.
    Exit 0 if all pass, 1 if any fail.

    Usage:
        modastack doctor
        modastack doctor --browser
        modastack doctor --browser --fix
    """
    from .doctor import run_doctor

    results = run_doctor()

    if browser:
        from . import browser as browser_mod
        if not browser_mod.is_linux():
            click.echo("Note: Chromium sandbox checks are Linux-specific; "
                       "running browser launch checks only.")
        results.extend(browser_mod.run_doctor())

    all_ok = True
    sandbox_failure = False
    for r in results:
        mark = "✓" if r.ok else "✗"
        click.echo(f"  {mark} {r.name}: {r.detail}")
        if not r.ok:
            all_ok = False
            if r.hint:
                click.echo(f"      → {r.hint}")
            if browser and hasattr(r, "sandbox_error") and r.sandbox_error:
                sandbox_failure = True

    if all_ok:
        click.echo("\nAll checks passed.")
        return

    if sandbox_failure and fix:
        from . import browser as browser_mod
        click.echo()
        _offer_sandbox_fix(browser_mod, non_interactive=False)
    elif sandbox_failure:
        click.echo("\nRe-run with `modastack doctor --browser --fix` to apply the sandbox fix.")

    raise SystemExit(1)


def _offer_sandbox_fix(browser_mod, non_interactive: bool) -> None:
    """Explain the Chromium sandbox issue and (interactively) apply the fix.

    Shared by `modastack setup` and `modastack doctor --fix`. In
    non-interactive mode it only prints instructions; otherwise it asks for
    confirmation before running the sudo sysctl change.
    """
    click.echo("Chromium's sandbox is blocked by the AppArmor restriction on")
    click.echo("unprivileged user namespaces — this prevents /browse from running.")
    click.echo()
    click.echo(f"  The fix:  {browser_mod.FIX_COMMAND}")
    click.echo(f"  Persisted in: {browser_mod.SYSCTL_CONF_PATH}")
    click.echo()
    click.echo("  Security tradeoff: this lets any local process create user")
    click.echo("  namespaces, a historical local-privilege-escalation surface.")
    click.echo("  Acceptable on dedicated dev machines. See deploy/INSTALL.md for")
    click.echo("  a narrower per-binary AppArmor alternative and the --no-sandbox fallback.")
    click.echo()

    if non_interactive:
        click.echo("  Non-interactive — apply it manually with the command above.")
        return

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
    """Agent management — launch, list, inspect, and cancel agents."""
    pass


@agents.command("list")
def agents_list():
    """List active agents.

    Usage:
        modastack agents list
    """
    from modastack.subagent import list_agents as _list_agents

    active = _list_agents()
    if not active:
        click.echo("No active agents.")
        return

    for a in active:
        state = "running" if a["running"] else "done"
        click.echo(f"  {a['issue_id']}/{a['phase']} — {state} ({a['elapsed_s']}s)")


@agents.command("show")
@click.argument("issue_id")
def agents_show(issue_id):
    """Show details for a specific agent.

    Usage:
        modastack agents show AGD-12
    """
    from modastack.subagent import list_agents as _list_agents, is_running, get_result

    if is_running(issue_id):
        for a in _list_agents():
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


@agents.command("cancel")
@click.argument("issue_id")
def agents_cancel(issue_id):
    """Cancel a running agent.

    Usage:
        modastack agents cancel AGD-12
    """
    from modastack.subagent import cancel_agent

    if cancel_agent(issue_id):
        click.echo(f"Cancelled {issue_id}")
    else:
        click.echo(f"No running agent for {issue_id}")


@main.command()
@click.option("--tail", default=20, help="Number of recent entries to show")
@click.option("--decisions-only", is_flag=True, help="Show only manager decisions")
def events(tail, decisions_only):
    """Show recent events and manager decisions as a unified timeline."""
    project_path = _detect_project_root()

    entries = []

    if not decisions_only:
        events_path = (project_path / ".modastack" / "state" / "events.jsonl") if project_path else None
        if events_path and events_path.exists():
            for line in events_path.read_text().strip().splitlines():
                entry = json.loads(line)
                data = entry.get("data", {})
                detail = data.get("text", "") or data.get("title", "") or data.get("issue_id", "")
                if len(detail) > 80:
                    detail = detail[:80] + "..."
                entries.append((
                    entry["timestamp"],
                    f"  {entry['timestamp']}  {entry['source']:8s}  {entry['type']}"
                    + (f"\n    {detail}" if detail else ""),
                ))

    decisions_path = (project_path / ".modastack" / "state" / "decisions.jsonl") if project_path else None
    if decisions_path and decisions_path.exists():
        for line in decisions_path.read_text().strip().splitlines():
            entry = json.loads(line)
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









@transcript.command("index")
@click.option("--project", default=None, help="Filter to project (substring match on path)")
def transcript_index(project):
    """Index conversation JSONL files into searchable SQLite.

    Scans ~/.claude/projects/*/conversations/ for JSONL files and indexes
    messages into a local SQLite database for fast searching.

    Usage:
        modastack transcript index                # index all projects
        modastack transcript index --project foo  # index only projects matching "foo"
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

    Searches message content using SQLite FTS. Requires `modastack transcript index`
    to have been run first.

    Usage:
        modastack transcript search "error handling"
        modastack transcript search "deploy" --project modastack --limit 5
    """
    from .history import search as do_search
    results = do_search(query, limit=limit, project=project)
    if not results:
        click.echo("No results. Run `modastack transcript index` first.")
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
        modastack transcript sessions
        modastack transcript sessions --limit 5 --project modastack
    """
    from .history import conversations
    convos = conversations(limit=limit, project=project)
    if not convos:
        click.echo("No conversations indexed. Run `modastack transcript index` first.")
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
    `modastack transcript sessions` to find session IDs.

    Usage:
        modastack transcript inspect abc12345
        modastack transcript inspect abc12345 --limit 10
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
    """List available workflow definitions.

    Scans two tiers (most specific wins):
      1. Project-local: <project>/.modastack/workflows/
      2. Built-in: <modastack>/workflows/

    Usage:
        modastack workflows list
    """
    from .workflow.triggers import WorkflowDispatcher

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    click.echo(dispatcher.format_workflow_menu())


@workflows.command("status")
def workflow_status():
    """Show active and recent workflow runs.

    Displays up to 20 recent runs with their status, trigger issue,
    node completion progress, and start time.

    Usage:
        modastack workflows status
    """
    from .workflow.state import WorkflowRun
    runs = WorkflowRun.list_runs()
    if not runs:
        click.echo("No workflow runs found.")
        return
    for run in runs[:20]:
        event_data = run.trigger_event.get("data", {})
        issue = event_data.get("issue_id", run.issue_id or "?")
        completed = sum(1 for ns in run.nodes.values() if ns.status == "completed")
        total = len(run.nodes)
        suffix = ""
        if run.status == "waiting" and run.await_event:
            suffix = f"  awaiting={run.await_event}"
        click.echo(f"  {run.run_id}  {run.workflow_name:20s} {run.status:10s} "
                  f"issue={issue}  {completed}/{total} nodes  {run.started_at[:19]}{suffix}")


@workflows.command("resume")
@click.argument("run_id")
@click.option("--timeout", default=3600, help="Max execution time in seconds")
def workflow_resume(run_id, timeout):
    """Resume a suspended workflow run.

    Picks up from the step after the await that suspended it.

    Usage:
        modastack workflows resume abc123
    """
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

    click.echo(f"Resuming {run.workflow_name} for {run.issue_id} "
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
        modastack workflows validate workflows/deploy.yaml
        modastack workflows validate myrepo/.modastack/workflows/deploy.yaml
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

    Scans two tiers (repo overrides built-in):
      1. Built-in: <modastack>/prompts/agents/
      2. Repo-local: <repo>/.modastack/agents/

    Usage:
        modastack roles list
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
        modastack monitors list
    """
    from .monitors.registry import MonitorRegistry

    registry = MonitorRegistry.load()
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
        click.echo(f"  {m.name:22s} {tier:16s} {m.interval:>5s}  {status:7s} "
                   f"{scope:16s} {m.event:30s} [{runner}]")


@monitors.command("add")
@click.argument("name")
@click.option("--interval", default="15m", help="How often to run (e.g. 5m, 15m, 1h)")
@click.option("--description", default="", help="What the monitor checks (interpreted by the manager)")
@click.option("--event", default=None, help="Synthetic event type to inject (default monitor/<name>)")
@click.option("--check", default="", help="Native check runner (pr_conflicts, stale_prs)")
@click.option("--url", default=None, help="URL the description references (e.g. deploy health)")
def monitor_add(name, interval, description, event, check, url):
    """Add a monitor to the current project.

    Usage:
        modastack monitors add "PR conflict check" --interval 15m \\
            --description "Check open PRs for merge conflicts"
        modastack monitors add deploy-health --interval 5m \\
            --url https://example.com
    """
    from .monitors.schema import Monitor, parse_interval
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    if not project_path:
        click.echo("Not inside a modastack project.", err=True)
        raise SystemExit(1)

    slug = _slugify(name)
    try:
        parse_interval(interval)
    except ValueError as e:
        raise click.ClickException(str(e))

    extra = {}
    if url:
        extra["url"] = url

    m = Monitor(
        name=slug,
        description=description,
        interval=interval,
        event=event or f"monitor/{slug}",
        check=check,
        extra=extra,
    )

    MonitorRegistry.add_project(m, project_path)
    click.echo(f"Added monitor '{slug}' to {project_path}/.modastack/monitors.yaml")
    click.echo(f"  interval={interval} event={m.event} "
               f"check={check or 'manager-interpreted'}")


@monitors.command("pause")
@click.argument("name")
def monitor_pause(name):
    """Disable a monitor (writes enabled: false).

    Usage:
        modastack monitors pause stale-pr-check
    """
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    if MonitorRegistry.pause(name, project_path):
        where = f"{project_path}/.modastack/monitors.yaml" if project_path else ".modastack/monitors.yaml"
        click.echo(f"Paused monitor '{name}' (enabled: false in {where})")
    else:
        click.echo(f"No monitor named '{name}' found.", err=True)
        raise SystemExit(1)


@monitors.command("remove")
@click.argument("name")
def monitor_remove(name):
    """Remove a monitor from the current project.

    Built-in defaults can't be deleted — pause them instead.

    Usage:
        modastack monitors remove deploy-health
    """
    from .monitors.registry import MonitorRegistry

    project_path = _detect_project_root()
    result = MonitorRegistry.remove(name, project_path)
    if result == "removed":
        click.echo(f"Removed monitor '{name}'.")
    elif result == "default-only":
        click.echo(f"'{name}' is a built-in default and can't be removed. "
                   f"Use `modastack monitors pause {name}` to disable it.", err=True)
        raise SystemExit(1)
    else:
        click.echo(f"No monitor named '{name}' found in a writable tier.", err=True)
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
    es_port = port or 8080

    if foreground:
        from modastack.events.server import ensure_running
        ensure_running(es_port, project_path=_detect_project_root())
        click.echo(f"Event server running on port {es_port} (foreground)")
        try:
            import time
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        return
    else:
        from modastack.events.server import ensure_running
        ensure_running(es_port, project_path=_detect_project_root())
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
        click.echo("Not inside a modastack project.", err=True)
        raise SystemExit(1)
    pid_file = _project_state_dir(project_path) / "event-server.pid"
    if not pid_file.exists():
        click.echo("Event server is not running")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Event server stopped (pid {pid})")
    except ProcessLookupError:
        click.echo("Event server was not running (stale PID file)")
    pid_file.unlink(missing_ok=True)


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
    import urllib.request
    es_port = 8080
    try:
        req = urllib.request.Request(f"http://localhost:{es_port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        click.echo(f"Event server: running on port {es_port}")
        click.echo(f"  Mode: {data.get('mode', 'unknown')}")
        click.echo(f"  Deployments: {data.get('deployments', 0)}")
    except Exception:
        click.echo(f"Event server: not running (port {es_port})")


main.add_command(event_server_cmd)


@agents.command("launch")
@click.option("--workflow", "-w", required=True, help="Workflow to run (e.g. issue-lifecycle, adhoc)")
@click.option("--role", required=True, help="Agent role (see 'modastack roles list')")
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
              help="Subscribe to event topics (e.g. moda-labs/modastack, slack:T123)")
def agents_launch(workflow, role, task, timeout, wait, post_event, requested_by, non_interactive, persistent, subscribe):
    """Launch an agent with a workflow and role.

    Every agent runs a workflow with a role. Use 'adhoc' for open-ended tasks.
    Use 'modastack roles list' to see available roles.

    Examples:
        modastack agents launch -w issue-lifecycle --role engineer --task "Work on #42"
        modastack agents launch -w adhoc --role engineer --task "Why is CI failing?"
        modastack agents launch -w adhoc --role engineer --task "Be a team lead" --persistent
        modastack agents launch -w adhoc --role manager --subscribe moda-labs/modastack --persistent
    """
    if subscribe:
        persistent = True
    _dispatch_agent(task=task, workflow=workflow, role=role,
                    timeout=timeout, wait=wait, post_event=post_event,
                    requested_by=requested_by,
                    interactive=not non_interactive,
                    persistent=persistent,
                    subscribe=list(subscribe))


def _dispatch_agent(*, task, workflow, role, timeout, wait, post_event, requested_by,
                    interactive=True, persistent=False, subscribe=None):
    """Dispatch logic for the agent command."""
    if not workflow:
        click.echo("--workflow is required. Use 'adhoc' for open-ended tasks.", err=True)
        raise SystemExit(1)

    if not task:
        task = f"Run workflow {workflow}"

    project_path = _detect_project_root()
    if not project_path:
        click.echo("Not inside a modastack project.", err=True)
        raise SystemExit(1)
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
    )
    click.echo(f"Agent started: {session_name}")



def _run_check(cwd: str, task: str, timeout: int, post_event: str | None) -> None:
    """Run a non-interactive check, print its verdict, optionally post an event.

    Used by `modastack spawn --non-interactive` and by the monitor scheduler,
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
    """Post a synthetic event to the event server's generic topic endpoint."""
    import urllib.error
    import urllib.request

    if "/" in event_type:
        source, etype = event_type.split("/", 1)
    else:
        source, etype = "monitor", event_type

    project_path = _detect_project_root()
    if not project_path:
        return False

    try:
        from modastack.config import ProjectConfig
        pc = ProjectConfig.from_file(project_path)
        es_url = pc.event_server_url or "http://localhost:8080"
    except Exception:
        es_url = "http://localhost:8080"

    payload = json.dumps({"source": source, "payload": data}).encode()
    try:
        req = urllib.request.Request(
            f"{es_url}/events/{etype}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return True
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        logging.getLogger(__name__).warning(f"Failed to post event {event_type}: {e}")
        return False


if __name__ == "__main__":
    main()
