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

from .config import GlobalConfig, GLOBAL_CONFIG_DIR

from .__version__ import __version__

LOG_PATH = GLOBAL_CONFIG_DIR / "modastack.log"
REPO_ROOT = Path(__file__).parent.parent

def _detect_repo_root(cwd: Path | None = None) -> Path | None:
    """Walk up from cwd to find a repo with .modastack/config.yaml."""
    path = (cwd or Path.cwd()).resolve()
    for candidate in [path, *path.parents]:
        if (candidate / ".modastack" / "config.yaml").exists():
            return candidate
        if (candidate / ".modastack.yaml").exists():
            return candidate
    return None


def _repo_state_dir(repo_path: Path) -> Path:
    """Runtime state directory for a repo's manager."""
    d = repo_path / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_dashboard_url(repo_path: Path | None = None) -> str:
    """Read the dashboard port from the repo's state dir."""
    if repo_path is None:
        repo_path = _detect_repo_root()
    if repo_path:
        port_file = repo_path / ".modastack" / "state" / "dashboard.port"
        if port_file.exists():
            try:
                port = int(port_file.read_text().strip())
                return f"http://localhost:{port}"
            except (ValueError, OSError):
                pass
    return "http://localhost:8095"



@click.group()
@click.version_option(version=__version__, prog_name="modastack")
def main():
    """Modastack — AI engineering manager + engineer team."""
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
    repo = _detect_repo_root()
    if repo:
        from modastack.sdk import set_repo_root
        set_repo_root(repo)


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


@main.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in the foreground (default: daemonize)")
def start(foreground):
    """Start modastack for the repo in the current directory.

    Usage:
        cd myrepo && modastack start              # daemonize
        cd myrepo && modastack start --foreground  # run in foreground
    """
    repo_path = _detect_repo_root()
    if not repo_path:
        click.echo("Not inside a modastack repo (no .modastack/config.yaml found). Run `modastack init` first.", err=True)
        raise SystemExit(1)

    if not foreground and _has_systemd_service():
        click.echo("Starting via systemd...")
        if _systemctl("start"):
            result = subprocess.run(
                ["systemctl", "--user", "show", "modastack", "--property=MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            pid = result.stdout.strip()
            click.echo(f"Modastack started (pid {pid}). Logs: {_repo_state_dir(repo_path) / 'manager.log'}")
        return

    state_dir = _repo_state_dir(repo_path)
    pid_path = state_dir / "manager.pid"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Modastack already running for {repo_path.name} (pid {pid}). Use `modastack restart`.")
            return
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)

    if foreground:
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers
                         if isinstance(h, logging.FileHandler)]

        from modastack.manager.events.consumer import run
        run(repo_path=repo_path)
    else:
        log_file = state_dir / "manager.log"
        env = os.environ.copy()
        venv_bin = str(Path(sys.executable).parent)
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{venv_bin}:{local_bin}:{env.get('PATH', '')}"
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "modastack.cli", "start", "--foreground"],
                stdout=lf, stderr=lf,
                cwd=str(repo_path),
                env=env,
                start_new_session=True,
            )
        click.echo(f"Modastack started for {repo_path.name} (pid {proc.pid}). Logs: {log_file}")


def _find_pid_path() -> Path | None:
    """Find the PID file for the current repo's manager."""
    repo_path = _detect_repo_root()
    if repo_path:
        p = _repo_state_dir(repo_path) / "manager.pid"
        if p.exists():
            return p
    legacy = GLOBAL_CONFIG_DIR / "modastack.pid"
    if legacy.exists():
        return legacy
    return None


@main.command()
@click.option("--force", is_flag=True, help="Send SIGKILL if SIGTERM doesn't work")
def stop(force):
    """Stop the modastack instance for the current repo.

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
def restart():
    """Stop and restart modastack.

    Usage:
        modastack restart
    """
    if _has_systemd_service():
        click.echo("Restarting via systemd...")
        _systemctl("restart")
        result = subprocess.run(
            ["systemctl", "--user", "show", "modastack", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        pid = result.stdout.strip()
        click.echo(f"Modastack restarted (pid {pid}). Logs: {GLOBAL_CONFIG_DIR / 'modastack.log'}")
        return

    ctx = click.get_current_context()
    ctx.invoke(stop)
    ctx.invoke(start)


@main.command()
@click.argument("text", required=True)
@click.option("--to", default=None, help="Target an engineer by issue ID (e.g. --to AGD-12)")
def message(text, to):
    """Send a message to the manager or an engineer.

    Usage:
        modastack message "what are you working on?"
        modastack message --to AGD-12 "try a different approach"
    """
    if to:
        click.echo(f"Note: engineer sub-agents run autonomously. "
                   f"To redirect {to}, cancel and re-run the phase.")
        return

    import urllib.request
    import urllib.error

    pid_path = _find_pid_path()
    if not pid_path:
        click.echo("Manager not running. Start with: modastack start")
        return
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        click.echo("Manager not running. Start with: modastack start")
        return

    try:
        import json as _json
        dashboard = _get_dashboard_url()
        req = urllib.request.Request(
            f"{dashboard}/api/message",
            data=_json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = _json.loads(resp.read())
        if result.get("ok"):
            click.echo(f"Sent: {text}")
        else:
            click.echo(f"Failed: {result.get('error', 'unknown')}", err=True)
    except urllib.error.URLError as e:
        click.echo(f"Cannot reach manager dashboard: {e}", err=True)


@main.command()
@click.argument("question", required=True)
@click.option("--timeout", default=300, type=int, help="Timeout in seconds")
@click.option("--source", default="engineer", help="Source identifier")
def consult(question, timeout, source):
    """Ask the manager a question and block until it responds.

    Used by engineer agents to get decisions, routing, or guidance
    from the manager. Prints the response to stdout.

    Usage:
        modastack consult "Should we use regex or string matching?"
        modastack consult "Draft a Slack message about the deploy" --timeout 60
    """
    import json as _json
    import uuid
    import urllib.request
    import urllib.error

    pid_path = _find_pid_path()
    if not pid_path:
        click.echo("Manager not running. Start with: modastack start", err=True)
        raise SystemExit(1)
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        click.echo("Manager not running. Start with: modastack start", err=True)
        raise SystemExit(1)

    payload = _json.dumps({
        "question": question,
        "correlation_id": str(uuid.uuid4()),
        "timeout": timeout,
        "source": source,
    }).encode()

    dashboard = _get_dashboard_url()
    try:
        req = urllib.request.Request(
            f"{dashboard}/api/consult",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            result = _json.loads(resp.read())

        if result.get("ok"):
            click.echo(result.get("response", ""))
        else:
            click.echo(f"Consultation failed: {result.get('error', 'unknown')}", err=True)
            raise SystemExit(1)

    except urllib.error.URLError as e:
        click.echo(f"Cannot reach manager dashboard: {e}", err=True)
        raise SystemExit(1)
    except TimeoutError:
        click.echo(f"Consultation timed out after {timeout}s", err=True)
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
    repo_path = _detect_repo_root()
    if repo_path and (repo_path / ".modastack" / "local.yaml").exists():
        from .config import LocalConfig
        local = LocalConfig.load(repo_path)
        token = local.slack_bot_token
    if not token:
        config = GlobalConfig.load()
        token = config.slack_token_for(workspace)
    if not token:
        click.echo(f"No bot token for workspace {workspace}", err=True)
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


@main.command()
@click.argument("session", default="manager")
@click.option("-n", "--lines", default=30, help="Number of recent messages to show")
@click.option("-f", "--follow", is_flag=True, help="Follow mode — stream new entries")
def log(session, lines, follow):
    """Show the full transcript for a session.

    Usage:
        modastack log manager             # manager transcript
        modastack log eng-70              # engineer transcript
        modastack log manager -n 50       # last 50 messages
        modastack log manager -f          # follow mode
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
        repo = _detect_repo_root()
        session = f"moda-mgr-{repo.name}" if repo else "moda-manager"

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

    repo_path = _detect_repo_root()
    running = False
    pid_path = _find_pid_path()
    if pid_path and pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    mgr_name = f"moda-mgr-{repo_path.name}" if repo_path else "moda-manager"
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


@main.command()
@click.argument("issue_id", required=False)
@click.option("--cancel", is_flag=True, help="Cancel a running engineer agent")
def engineers(issue_id, cancel):
    """List active engineers, or inspect/cancel a specific one.

    Usage:
        modastack engineers              # list all active
        modastack engineers AGD-12       # show details for AGD-12
        modastack engineers AGD-12 --cancel  # cancel AGD-12
    """
    from modastack.subagent import list_agents, cancel_agent, is_running, get_result

    if issue_id and cancel:
        if cancel_agent(issue_id):
            click.echo(f"Cancelled {issue_id}")
        else:
            click.echo(f"No running agent for {issue_id}")
        return

    if issue_id:
        if is_running(issue_id):
            agents = list_agents()
            for a in agents:
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
        return

    agents = list_agents()
    if not agents:
        click.echo("No active engineers.")
        return

    for agent in agents:
        state = "running" if agent["running"] else "done"
        click.echo(f"  {agent['issue_id']}/{agent['phase']} — {state} ({agent['elapsed_s']}s)")


@main.command()
@click.option("--tail", default=20, help="Number of recent events to show")
def events(tail):
    """Show recent events from the event bus."""
    repo_path = _detect_repo_root()
    events_path = (repo_path / ".modastack" / "state" / "events.jsonl") if repo_path else None
    if not events_path or not events_path.exists():
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
    repo_path = _detect_repo_root()
    decisions_path = (repo_path / ".modastack" / "state" / "decisions.jsonl") if repo_path else None
    if not decisions_path or not decisions_path.exists():
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
@click.option("--non-interactive", is_flag=True, envvar="CI")
def init(non_interactive):
    """Initialize global config."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    if not config.webhook_secret:
        import secrets
        config.webhook_secret = f"whsec_{secrets.token_hex(24)}"
        config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")
    click.echo(f"Webhook secret: {config.webhook_secret}")
    click.echo(f"  Use this as your GitHub webhook secret")




@main.command()
@click.option("--port", default=8095, help="Dashboard server port")
def dashboard(port):
    """Start the web dashboard."""
    from dashboard.app import run_dashboard
    run_dashboard(port=port)


@main.group()
def history():
    """Conversation history — index and search Claude Code sessions."""
    pass


@history.command("index")
@click.option("--project", default=None, help="Filter to project (substring match on path)")
def history_index(project):
    """Index conversation JSONL files into searchable SQLite.

    Scans ~/.claude/projects/*/conversations/ for JSONL files and indexes
    messages into a local SQLite database for fast searching.

    Usage:
        modastack history index                # index all projects
        modastack history index --project foo   # index only projects matching "foo"
    """
    from .history import index as do_index
    click.echo("Indexing conversations...")
    stats = do_index(project_filter=project)
    click.echo(f"  Scanned {stats['files_scanned']} files, {stats['files_with_new']} had new data")
    click.echo(f"  Indexed {stats['new_messages']} new messages")
    click.echo(f"  Total: {stats['total_conversations']} conversations, {stats['total_messages']} messages")


@history.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Max results")
@click.option("--project", default=None, help="Filter to project")
def history_search(query, limit, project):
    """Full-text search across indexed conversation history.

    Searches message content using SQLite FTS. Requires `modastack history index`
    to have been run first.

    Usage:
        modastack history search "error handling"
        modastack history search "deploy" --project modastack --limit 5
    """
    from .history import search as do_search
    results = do_search(query, limit=limit, project=project)
    if not results:
        click.echo("No results. Run `modastack history index` first.")
        return
    for r in results:
        branch = r.get("git_branch") or ""
        role = r.get("role") or r.get("type") or ""
        tool = f" [{r['tool_name']}]" if r.get("tool_name") else ""
        snippet = (r.get("snippet") or "")[:200].replace("\n", " ")
        click.echo(f"  {r['timestamp'][:19]}  {role:10s}{tool}  {branch}")
        click.echo(f"    {snippet}")
        click.echo()


@history.command("sessions")
@click.option("--limit", default=20)
@click.option("--project", default=None)
def history_sessions(limit, project):
    """List indexed conversations with metadata.

    Shows session ID, git branch, message count, and working directory for
    each indexed conversation. Use session IDs with `modastack history show`.

    Usage:
        modastack history sessions
        modastack history sessions --limit 5 --project modastack
    """
    from .history import conversations
    convos = conversations(limit=limit, project=project)
    if not convos:
        click.echo("No conversations indexed. Run `modastack history index` first.")
        return
    for c in convos:
        branch = c.get("git_branch") or ""
        click.echo(f"  {c['started_at'][:19]}  {c['session_id'][:8]}  {branch:20s}  {c['message_count']} msgs  {c.get('cwd', '')}")


@history.command("show")
@click.argument("session_id")
@click.option("--limit", default=50)
def history_show(session_id, limit):
    """Show messages from a specific session.

    Accepts a full or partial session ID (prefix match). Use
    `modastack history sessions` to find session IDs.

    Usage:
        modastack history show abc12345
        modastack history show abc12345 --limit 10
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


main.add_command(history)


@main.group()
def workflow():
    """Workflow engine — manage YAML-based DAG workflows."""
    pass


@workflow.command("list")
def workflow_list():
    """List available workflow definitions.

    Scans two tiers (most specific wins):
      1. Repo-local: <repo>/.modastack/workflows/
      2. Built-in: <modastack>/workflows/

    Usage:
        modastack workflow list
    """
    from .workflow.triggers import WorkflowDispatcher

    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    click.echo(dispatcher.format_workflow_menu())


@workflow.command("status")
def workflow_status():
    """Show active and recent workflow runs.

    Displays up to 20 recent runs with their status, trigger issue,
    node completion progress, and start time.

    Usage:
        modastack workflow status
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


@workflow.command("resume")
@click.argument("run_id")
@click.option("--timeout", default=3600, help="Max execution time in seconds")
def workflow_resume(run_id, timeout):
    """Resume a suspended workflow run.

    Picks up from the step after the await that suspended it.

    Usage:
        modastack workflow resume abc123
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


@workflow.command("validate")
@click.argument("path", type=click.Path(exists=True))
def workflow_validate(path):
    """Validate a workflow YAML file.

    Parses the YAML, checks the DAG structure, reports variable scopes used,
    and prints the topological execution order if valid.

    Usage:
        modastack workflow validate workflows/deploy.yaml
        modastack workflow validate myrepo/.modastack/workflows/deploy.yaml
    """
    import re
    from .workflow.schema import load_workflow
    try:
        wf = load_workflow(Path(path))
        order = wf.topological_order()
        click.echo(f"Valid: {wf.name} v{wf.version} ({len(wf.nodes)} nodes)")
        click.echo(f"Trigger: {wf.trigger.event}")
        if wf.trigger.filter:
            click.echo(f"Filter: {wf.trigger.filter}")
        click.echo(f"Execution order: {' -> '.join(order)}")

        # Report variable scopes referenced
        raw = Path(path).read_text()
        refs = set(re.findall(r'\$\{\{(\w+)\.', raw))
        builtin_scopes = {"event", "config", "repo", "handoff"} | set(wf.nodes.keys())
        unknown = refs - builtin_scopes
        click.echo(f"Variable scopes: {', '.join(sorted(refs))}")
        if unknown:
            click.echo(f"Warning: unknown scopes (may be node outputs): {', '.join(sorted(unknown))}")

        # Show node types breakdown
        from collections import Counter
        type_counts = Counter(n.type.value for n in wf.nodes.values())
        click.echo(f"Node types: {', '.join(f'{t}={c}' for t, c in sorted(type_counts.items()))}")

    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        raise SystemExit(1)




main.add_command(workflow)


@main.group()
def role():
    """Agent roles — list available role prompts."""
    pass


@role.command("list")
@click.option("--repo", default=None, help="Include repo-specific roles from this repo")
def role_list(repo):
    """List available agent roles.

    Scans two tiers (repo overrides built-in):
      1. Built-in: <modastack>/prompts/agents/
      2. Repo-local: <repo>/.modastack/agents/

    Usage:
        modastack role list
        modastack role list --repo jobtack
    """
    from .prompts.resolver import discover_roles, format_role_list

    repo_path = None
    if repo:
        repo_path = Path(_resolve_repo(repo))
    roles = discover_roles(repo_path)
    click.echo(format_role_list(roles))


main.add_command(role)


@main.group()
def monitor():
    """Background monitoring tasks — scheduled polling to fill webhook gaps."""
    pass


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "monitor"


def _resolve_monitor_repo(repo: str) -> Path:
    """Resolve a --repo argument (path or registered name) to a Path."""
    candidate = Path(repo).expanduser()
    if candidate.exists():
        return candidate.resolve()
    config = GlobalConfig.load()
    for registered in config.repos:
        if registered.name == repo or str(registered) == repo:
            return registered
        if "/" in repo and registered.name == repo.split("/")[-1]:
            return registered
    raise click.ClickException(f"Repo not found: {repo} (not a path or registered repo)")


@monitor.command("list")
def monitor_list():
    """Show the merged view of monitors across all tiers, with source.

    Usage:
        modastack monitor list
    """
    from .monitors.registry import MonitorRegistry

    registry = MonitorRegistry.load()
    monitors = sorted(registry.all_monitors(), key=lambda m: (m.name, m.repo))
    if not monitors:
        click.echo("No monitors found.")
        return

    for m in monitors:
        if m.source == "default":
            tier = "default"
        elif m.source == "user":
            tier = "user"
        else:
            tier = f"repo:{Path(m.source).name}"
        status = "active" if m.enabled else "paused"
        scope = Path(m.repo).name if m.repo else "all repos"
        runner = m.check or "manager"
        click.echo(f"  {m.name:22s} {tier:16s} {m.interval:>5s}  {status:7s} "
                   f"{scope:16s} {m.event:30s} [{runner}]")


@monitor.command("add")
@click.argument("name")
@click.option("--interval", default="15m", help="How often to run (e.g. 5m, 15m, 1h)")
@click.option("--description", default="", help="What the monitor checks (interpreted by the manager)")
@click.option("--event", default=None, help="Synthetic event type to inject (default monitor/<name>)")
@click.option("--check", default="", help="Native check runner (pr_conflicts, stale_prs)")
@click.option("--url", default=None, help="URL the description references (e.g. deploy health)")
@click.option("--repo", default=None, help="Scope to a repo (path or name); else applies globally")
def monitor_add(name, interval, description, event, check, url, repo):
    """Add a monitor, routing it to the right writable storage tier.

    With --repo it lands in that repo's .modastack.yaml; otherwise in
    ~/.modastack/monitors.yaml (applies across all repos).

    Usage:
        modastack monitor add "PR conflict check" --interval 15m \\
            --description "Check open PRs for merge conflicts"
        modastack monitor add deploy-health --interval 5m \\
            --url https://example.com --repo jobtack
    """
    from .monitors.schema import Monitor, parse_interval
    from .monitors.registry import MonitorRegistry

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

    if repo:
        repo_path = _resolve_monitor_repo(repo)
    else:
        repo_path = _detect_repo_root()
        if not repo_path:
            click.echo("Not inside a modastack repo. Use --repo or run from inside a repo.", err=True)
            raise SystemExit(1)
    MonitorRegistry.add_repo(m, repo_path)
    click.echo(f"Added monitor '{slug}' to {repo_path}/.modastack/monitors.yaml")
    click.echo(f"  interval={interval} event={m.event} "
               f"check={check or 'manager-interpreted'}")


@monitor.command("pause")
@click.argument("name")
@click.option("--repo", default=None, help="Pause only for a specific repo")
def monitor_pause(name, repo):
    """Disable a monitor (writes enabled: false to a writable tier).

    Usage:
        modastack monitor pause stale-pr-check
        modastack monitor pause pr-conflict-check --repo jobtack
    """
    from .monitors.registry import MonitorRegistry

    repo_path = _resolve_monitor_repo(repo) if repo else None
    if MonitorRegistry.pause(name, repo_path):
        where = f"{repo_path}/.modastack/monitors.yaml" if repo_path else "~/.modastack/monitors.yaml"
        click.echo(f"Paused monitor '{name}' (enabled: false in {where})")
    else:
        click.echo(f"No monitor named '{name}' found.", err=True)
        raise SystemExit(1)


@monitor.command("remove")
@click.argument("name")
@click.option("--repo", default=None, help="Remove from a specific repo's config")
def monitor_remove(name, repo):
    """Remove a monitor from a user-writable tier.

    Built-in defaults can't be deleted — pause them instead.

    Usage:
        modastack monitor remove deploy-health
    """
    from .monitors.registry import MonitorRegistry

    repo_path = _resolve_monitor_repo(repo) if repo else None
    result = MonitorRegistry.remove(name, repo_path)
    if result == "removed":
        click.echo(f"Removed monitor '{name}'.")
    elif result == "default-only":
        click.echo(f"'{name}' is a built-in default and can't be removed. "
                   f"Use `modastack monitor pause {name}` to disable it.", err=True)
        raise SystemExit(1)
    else:
        click.echo(f"No monitor named '{name}' found in a writable tier.", err=True)
        raise SystemExit(1)


main.add_command(monitor)


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
    config = GlobalConfig.load()
    es_port = port or config.webhook_port

    if foreground:
        from modastack.manager.events.event_server import run_server
        run_server(es_port, config.webhook_secret, config.slack_signing_secret)
    else:
        from modastack.manager.events.event_server import ensure_running
        ensure_running(es_port, config.webhook_secret, config.slack_signing_secret)
        click.echo(f"Event server running on port {es_port}")
        click.echo(f"  GitHub:  http://localhost:{es_port}/webhooks/github")
        click.echo(f"  Linear:  http://localhost:{es_port}/webhooks/linear")
        click.echo(f"  Slack:   http://localhost:{es_port}/webhooks/slack")


@event_server_cmd.command("stop")
def event_server_stop():
    """Stop the local event server."""
    import signal
    pid_file = GLOBAL_CONFIG_DIR / "event-server.pid"
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
    config = GlobalConfig.load()
    es_port = config.webhook_port
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


@main.command()
@click.version_option(version=__version__, prog_name="modastack agent")
@click.option("--repo", default=None, help="Repo path or registered name")
@click.option("--workflow", "-w", required=True, help="Workflow to run (e.g. issue-lifecycle, adhoc)")
@click.option("--role", required=True, help="Agent role (see 'modastack role list')")
@click.option("--task", default=None, help="Task description / context for the agent")
@click.option("--timeout", default=3600, type=int, help="Timeout in seconds")
@click.option("--wait", is_flag=True, help="Block until the agent completes")
@click.option("--post-event", "post_event", default=None,
              help="Post this event type on completion (for --wait checks)")
@click.option("--requested-by", "requested_by", default=None,
              help='JSON identity of requester, e.g. \'{"from":"Alice","channel":"C1"}\'')
@click.option("--non-interactive", "non_interactive", is_flag=True,
              help="Run without manager — agent makes all decisions autonomously")
def agent(repo, workflow, role, task, timeout, wait, post_event, requested_by, non_interactive):
    """Launch an agent with a workflow and role.

    Every agent runs a workflow with a role. Use 'adhoc' for open-ended tasks.
    Use 'modastack role list' to see available roles.

    Examples:
        modastack agent -w issue-lifecycle --role engineer --repo jobtack --task "Work on #42"
        modastack agent -w adhoc --role engineer --repo jobtack --task "Why is CI failing?"
        modastack agent -w adhoc --role engineer --non-interactive --repo jobtack --task "Fix the bug"
    """
    _dispatch_agent(repo=repo, task=task, workflow=workflow, role=role,
                    timeout=timeout, wait=wait, post_event=post_event,
                    requested_by=requested_by,
                    interactive=not non_interactive)


def _dispatch_agent(*, repo, task, workflow, role, timeout, wait, post_event, requested_by, interactive=True):
    """Dispatch logic for the agent command."""
    if not workflow:
        click.echo("--workflow is required. Use 'adhoc' for open-ended tasks.", err=True)
        raise SystemExit(1)

    if not task:
        task = f"Run workflow {workflow}"

    # --- Wait / check mode ---
    if wait:
        cwd = _resolve_repo(repo) if repo else os.getcwd()
        _run_check(cwd=cwd, task=task, timeout=timeout, post_event=post_event)
        return

    cwd = _resolve_repo(repo)
    if not cwd:
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
    )
    click.echo(f"Agent started: {session_name}")


def _resolve_repo(repo: str | None) -> str | None:
    """Resolve a repo flag to a local path."""
    if not repo:
        click.echo("--repo is required.", err=True)
        raise SystemExit(1)
    path = Path(repo).expanduser()
    if path.is_dir():
        return str(path.resolve())
    config = GlobalConfig.load()
    for rp in config.repos:
        if rp.name == repo or str(rp) == repo:
            return str(rp)
        if "/" in repo and rp.name == repo.split("/")[-1]:
            return str(rp)
    click.echo(f"Repo not found: {repo}", err=True)
    raise SystemExit(1)


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
    """Post a synthetic event onto the running manager's event bus over HTTP.

    The check runs in its own process, so it can't reach the in-memory event
    queue directly — it posts to the local dashboard, which enqueues it on the
    same queue webhooks use.
    """
    import urllib.error
    import urllib.request

    if "/" in event_type:
        source, etype = event_type.split("/", 1)
    else:
        source, etype = "monitor", event_type

    payload = json.dumps({"type": etype, "source": source, "data": data}).encode()
    try:
        dashboard = _get_dashboard_url()
        req = urllib.request.Request(
            f"{dashboard}/api/event",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return bool(result.get("ok"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        logging.getLogger(__name__).warning(f"Failed to post event {event_type}: {e}")
        return False


if __name__ == "__main__":
    main()
