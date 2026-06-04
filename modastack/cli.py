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
from .setup import generate_dispatch_yaml
from .__version__ import __version__

LOG_PATH = GLOBAL_CONFIG_DIR / "modastack.log"
UPDATE_STATE_PATH = GLOBAL_CONFIG_DIR / "update_state.json"
REPO_ROOT = Path(__file__).parent.parent

HOOK_SETTINGS = {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": ".claude/hooks/session-state.sh", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": ".claude/hooks/session-state.sh", "timeout": 5}]}],
}


def install_hooks(target_path: Path) -> list[str]:
    """Install Claude Code hooks for session state tracking.

    Copies the hook script and merges hook config into .claude/settings.json.
    Skips if target is the modastack repo itself.
    Returns list of actions taken.
    """
    actions = []
    repo_root = Path(__file__).parent.parent

    if target_path.resolve() == repo_root.resolve():
        return actions

    # Copy hook script
    hooks_dir = target_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    src_hook = repo_root / ".claude" / "hooks" / "session-state.sh"
    dst_hook = hooks_dir / "session-state.sh"

    if src_hook.exists() and src_hook.resolve() != dst_hook.resolve():
        import shutil
        shutil.copy2(src_hook, dst_hook)
        dst_hook.chmod(0o755)
        actions.append("Installed .claude/hooks/session-state.sh")

    # Merge hooks into settings.json
    settings_path = target_path / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass

    existing_hooks = settings.get("hooks", {})
    changed = False
    for event_name, event_config in HOOK_SETTINGS.items():
        if event_name not in existing_hooks:
            existing_hooks[event_name] = event_config
            changed = True

    if changed:
        settings["hooks"] = existing_hooks
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        actions.append("Configured hooks in .claude/settings.json")

    return actions


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
    """Start modastack. Connects to the centralized event server for webhooks.

    Usage:
        modastack start              # daemonize
        modastack start --foreground # run in foreground (for debugging)
    """
    if not foreground and _has_systemd_service():
        click.echo("Starting via systemd...")
        if _systemctl("start"):
            result = subprocess.run(
                ["systemctl", "--user", "show", "modastack", "--property=MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            pid = result.stdout.strip()
            click.echo(f"Modastack started (pid {pid}). Logs: {GLOBAL_CONFIG_DIR / 'modastack.log'}")
        return

    pid_path = GLOBAL_CONFIG_DIR / "modastack.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Modastack already running (pid {pid}). Use `modastack restart`.")
            return
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)

    if foreground:
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers
                         if isinstance(h, logging.FileHandler)]

        from modastack.manager.events.consumer import run
        run()
    else:
        log_file = GLOBAL_CONFIG_DIR / "modastack.log"
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "modastack.cli", "start", "--foreground"],
                stdout=lf, stderr=lf,
                start_new_session=True,
            )
        click.echo(f"Modastack started (pid {proc.pid}). Logs: {log_file}")


@main.command()
@click.option("--force", is_flag=True, help="Send SIGKILL if SIGTERM doesn't work")
def stop(force):
    """Stop a running modastack instance.

    Usage:
        modastack stop
        modastack stop --force
    """
    if _has_systemd_service() and not force:
        click.echo("Stopping via systemd...")
        _systemctl("stop")
        return

    import signal

    pid_path = GLOBAL_CONFIG_DIR / "modastack.pid"
    if not pid_path.exists():
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

    pid_path = GLOBAL_CONFIG_DIR / "modastack.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            click.echo("Manager not running. Start with: modastack start")
            return
    else:
        click.echo("Manager not running. Start with: modastack start")
        return

    try:
        import json as _json
        req = urllib.request.Request(
            "http://localhost:8095/api/message",
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

    pid_path = GLOBAL_CONFIG_DIR / "modastack.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            click.echo("Manager not running. Start with: modastack start", err=True)
            raise SystemExit(1)
    else:
        click.echo("Manager not running. Start with: modastack start", err=True)
        raise SystemExit(1)

    payload = _json.dumps({
        "question": question,
        "correlation_id": str(uuid.uuid4()),
        "timeout": timeout,
        "source": source,
    }).encode()

    try:
        req = urllib.request.Request(
            "http://localhost:8095/api/consult",
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
    from modastack.sdk import SESSION_DIR, SessionRegistry, get_registry

    if session == "manager":
        from modastack.manager.session import get_default_session
        s = get_default_session()
        session = s.session_name if s else "moda-manager"

    # Primary: session dir log
    session_log = SessionRegistry.log_path(session)
    if session_log.exists():
        return session_log

    # Fallback: Claude Code transcript via session ID
    id_file = SESSION_DIR / f"{session}.id"
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
    recent_dirs = sorted(
        [d for d in SESSION_DIR.iterdir() if d.is_dir() and (d / "state.json").exists()],
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


@main.command(hidden=True)
@click.argument("text", required=False)
def tick(text):
    """Deprecated: use 'modastack message' instead."""
    ctx = click.get_current_context()
    if text:
        ctx.invoke(message, text=text)
    else:
        from modastack.manager.session import is_alive, detect_state
        state = detect_state() if is_alive() else "stopped"
        click.echo(f"Manager state: {state}")
        click.echo("Hint: use 'modastack message' to send, 'modastack log' to read.")


@main.command()
def status():
    """Show active agents — manager + engineer sub-agents."""
    from modastack.sdk import load_session_id, get_registry

    pid_path = GLOBAL_CONFIG_DIR / "modastack.pid"
    running = False
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    if not running:
        import subprocess as _sp
        try:
            result = _sp.run(
                ["pgrep", "-f", "modastack.*start"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                    os.kill(pid, 0)
                    running = True
                    break
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except (FileNotFoundError, _sp.TimeoutExpired):
            pass

    from modastack.manager.session import get_default_session
    mgr = get_default_session()
    mgr_name = mgr.session_name if mgr else "moda-manager"
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
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--task-tracking", type=click.Choice(["github-issues", "linear"]), default=None)
@click.option("--project", default=None, help="Project prefix (e.g., BET, TESS)")
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None)
def register(repo_path: str, task_tracking: str | None, project: str | None, linear_key: str | None):
    """Register a repo with modastack (alias for setup)."""
    from click import Context
    ctx = click.get_current_context()
    ctx.invoke(setup, repo_path=repo_path, task_tracking=task_tracking,
               project=project, linear_key=linear_key, non_interactive=True)


@main.command()
@click.option("--non-interactive", is_flag=True, envvar="CI")
def init(non_interactive):
    """Initialize global config and install modastack to PATH."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = GlobalConfig.load()
    config.save()
    click.echo(f"Config initialized at {GLOBAL_CONFIG_DIR / 'config.yaml'}")

    _install_to_path()

    click.echo("Run `modastack setup <repo>` to add a repo.")


def _install_to_path():
    """Symlink the modastack binary into ~/.local/bin and ensure it's on PATH.

    Adds the PATH export above the interactive guard in .bashrc so it
    works for both interactive shells and non-interactive SSH commands.
    """
    import shutil

    venv_bin = shutil.which("modastack")
    if not venv_bin:
        return

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    link = local_bin / "modastack"

    if link.exists() or link.is_symlink():
        if link.resolve() == Path(venv_bin).resolve():
            click.echo(f"Already installed: {link}")
            return
        link.unlink()

    link.symlink_to(venv_bin)
    click.echo(f"Installed: {link} -> {venv_bin}")

    if str(local_bin) not in os.environ.get("PATH", ""):
        _ensure_path_in_bashrc(str(local_bin))


def _ensure_path_in_bashrc(bin_dir: str):
    """Add bin_dir to PATH in .bashrc, above the interactive guard."""
    bashrc = Path.home() / ".bashrc"
    export_line = f'export PATH="$HOME/.local/bin:$PATH"'

    if bashrc.exists():
        content = bashrc.read_text()
        if ".local/bin" in content:
            return
        # Insert before the interactive guard so non-interactive SSH picks it up
        guard = "# If not running interactively"
        if guard in content:
            content = content.replace(guard, export_line + "\n\n" + guard)
            bashrc.write_text(content)
            click.echo(f"Added {bin_dir} to PATH in ~/.bashrc")
            return

    # No bashrc or no guard — just append
    with open(bashrc, "a") as f:
        f.write(f"\n{export_line}\n")
    click.echo(f"Added {bin_dir} to PATH in ~/.bashrc")


@main.command()
def repos():
    """List registered repos."""
    config = GlobalConfig.load()
    if not config.repos:
        click.echo("No repos registered.")
        return
    for path in config.repos:
        has_config = (
            (path / ".modastack" / "config.yaml").exists()
            or (path / ".modastack.yaml").exists()
        )
        click.echo(f"  {path.name:30s} [{'ready' if has_config else 'no config'}] {path}")


EVENT_SERVER_URL = "https://modastack-events.modalabs.workers.dev"


@main.command()
@click.argument("repo_path", type=click.Path(exists=True), default=".")
@click.option("--task-tracking", type=click.Choice(["github-issues", "linear"]), default=None,
              help="Task tracking system (default: github-issues)")
@click.option("--project", default=None, help="Project prefix (e.g., BET, TESS)")
@click.option("--linear-key", envvar="LINEAR_API_KEY", default=None, help="Linear API key (only for --task-tracking linear)")
@click.option("--non-interactive", is_flag=True, envvar="CI")
def setup(repo_path: str, task_tracking: str | None, project: str | None,
          linear_key: str | None, non_interactive: bool):
    """Set up a repo for modastack."""
    import yaml

    path = Path(repo_path).resolve()
    config_dir = path / ".modastack"
    config_path = config_dir / "config.yaml"
    credential_name = path.name

    if config_path.exists() and not non_interactive:
        try:
            if not click.confirm(f".modastack/config.yaml exists in {path}. Overwrite?"):
                return
        except (EOFError, click.Abort):
            pass

    # Default to github-issues
    if not task_tracking:
        task_tracking = "linear" if linear_key else "github-issues"

    # Handle credentials for Linear
    if task_tracking == "linear":
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

    config = generate_dispatch_yaml(path, task_tracking=task_tracking)
    config["credentials"] = credential_name
    if project:
        config["task_tracking"]["project"] = project

    # Check if config is already tracked BEFORE writing
    is_new_file = not config_path.exists() or subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".modastack/config.yaml"],
        capture_output=True, cwd=path,
    ).returncode != 0

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    click.echo(f"Generated: {config_path}")

    # Open a PR to commit .modastack/config.yaml if it's new
    if is_new_file:
        _open_config_pr(path)

    # Auto-register in global config (legacy) and repos.json (discovery)
    global_config = GlobalConfig.load()
    if path not in global_config.repos:
        global_config.repos.append(path)
        global_config.save()
        click.echo("Registered.")
    _register_repo_discovery(path)

    # Bootstrap task tracker
    if task_tracking == "linear":
        resolved_key = linear_key
        if not resolved_key:
            from .config import Credentials
            resolved_key = (Credentials.load().get(credential_name) or {}).get("linear_api_key")
        resolved_project = project or config["task_tracking"]["project"]
        if resolved_key and resolved_project:
            click.echo("Bootstrapping Linear board...")
            from .board_setup import bootstrap_board
            for action in bootstrap_board(resolved_key, resolved_project):
                click.echo(f"  {action}")
    elif task_tracking == "github-issues":
        click.echo("Bootstrapping GitHub Issues labels...")
        from .github_issues import bootstrap_labels
        for action in bootstrap_labels(path):
            click.echo(f"  {action}")

    # GitHub App: check installation and prompt if missing
    if task_tracking == "github-issues":
        _ensure_github_app(path, non_interactive)

    # Event server: register deployment and subscribe to this repo
    _ensure_event_server(path, global_config)

    # Create .modastack/local.yaml with operator-specific config
    from .config import LocalConfig
    local = LocalConfig.load(path)
    if linear_key and not local.credentials.get("linear_api_key"):
        local.credentials["linear_api_key"] = linear_key
    if global_config.event_server_deployment_id and not local.event_server_deployment_id:
        local.event_server_url = global_config.event_server_url
        local.event_server_deployment_id = global_config.event_server_deployment_id
        local.event_server_api_key = global_config.event_server_api_key
    if global_config.slack_bot_token and not local.slack_bot_token:
        local.slack_bot_token = global_config.slack_bot_token
    if global_config.slack_dm_channel and not local.slack_dm_channel:
        local.slack_dm_channel = global_config.slack_dm_channel
    local.save(path)
    click.echo(f"Generated: {path / '.modastack' / 'local.yaml'}")

    # Add local state to .gitignore
    gitignore_path = path / ".gitignore"
    gitignore_entries = ["worktrees/", ".modastack/local.yaml", ".modastack/state/"]
    existing = gitignore_path.read_text() if gitignore_path.exists() else ""
    added = []
    for entry in gitignore_entries:
        if entry not in existing:
            added.append(entry)
    if added:
        with open(gitignore_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(added) + "\n")
        click.echo(f"Added to .gitignore: {', '.join(added)}")

    # Install hooks
    hook_actions = install_hooks(path)
    for action in hook_actions:
        click.echo(f"  {action}")

    # Verify /browse can launch Chromium; offer the sandbox fix if it can't.
    _check_browser_sandbox(non_interactive)

    click.echo(f"Ready — {path.name} is set up for modastack.")


def _check_browser_sandbox(non_interactive: bool) -> None:
    """Detect the Chromium sandbox block during setup and offer the fix.

    Runs a quick headless Chromium launch. If it fails specifically because of
    the AppArmor user-namespace restriction, explains the issue and (when
    interactive) offers to apply the sysctl fix. Other failures are reported as
    a hint without blocking setup, since /browse is optional tooling.
    """
    from . import browser as browser_mod

    # The sandbox restriction is Linux-only; skip elsewhere.
    if not browser_mod.is_linux():
        return

    click.echo("Checking Chromium sandbox for /browse...")
    result = browser_mod.check_chromium_launch()
    if result.ok:
        click.echo("  Chromium launches — /browse is ready.")
        return

    if result.sandbox_error:
        _offer_sandbox_fix(browser_mod, non_interactive)
    else:
        # Don't block setup on unrelated browser issues — just surface it.
        click.echo(f"  /browse check skipped: {result.detail}")
        if result.hint:
            click.echo(f"    → {result.hint}")
        click.echo("    Run `modastack doctor` for a full diagnosis.")


def _open_config_pr(path: Path) -> None:
    """Create a branch, commit .modastack.yaml, push, and open a PR."""
    branch = "chore/add-modastack-config"

    def _run(*cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=path, **kwargs)

    # Determine default branch
    result = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
    original_branch = result.stdout.strip() if result.returncode == 0 else "main"

    # Create and switch to the new branch
    result = _run("git", "checkout", "-b", branch)
    if result.returncode != 0:
        click.echo(f"  Could not create branch '{branch}': {result.stderr.strip()}")
        return

    # Stage and commit
    _run("git", "add", ".modastack/config.yaml")
    result = _run("git", "commit", "-m", "chore: add .modastack/config.yaml for modastack integration")
    if result.returncode != 0:
        click.echo(f"  Commit failed: {result.stderr.strip()}")
        _run("git", "checkout", original_branch)
        return

    # Push
    result = _run("git", "push", "-u", "origin", branch)
    if result.returncode != 0:
        click.echo(f"  Push failed: {result.stderr.strip()}")
        _run("git", "checkout", original_branch)
        return

    # Open PR
    result = _run(
        "gh", "pr", "create",
        "--title", "chore: add modastack config",
        "--body", "Adds modastack configuration generated by `modastack setup`.\n\n"
                  "This file configures task tracking and agent integration for this repository.",
    )
    if result.returncode == 0:
        pr_url = result.stdout.strip()
        click.echo(f"  PR opened: {pr_url}")
    else:
        click.echo(f"  PR creation failed: {result.stderr.strip()}")

    # Switch back to original branch
    _run("git", "checkout", original_branch)


REPOS_JSON_PATH = GLOBAL_CONFIG_DIR / "repos.json"


def _register_repo_discovery(path: Path) -> None:
    """Append a repo to ~/.modastack/repos.json for discoverability."""
    repos: list[str] = []
    if REPOS_JSON_PATH.exists():
        try:
            repos = json.loads(REPOS_JSON_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    path_str = str(path.resolve())
    if path_str not in repos:
        repos.append(path_str)
        REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPOS_JSON_PATH.write_text(json.dumps(repos, indent=2) + "\n")


def _get_repo_full_name(path: Path) -> str:
    """Get owner/repo from git remote."""
    if not path.exists():
        return ""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=path,
    )
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    # Handle SSH (git@github.com:owner/repo.git) and HTTPS
    if ":" in url and "@" in url:
        path_part = url.split(":")[-1]
    else:
        path_part = "/".join(url.split("/")[-2:])
    return path_part.removesuffix(".git")


def _ensure_github_app(path: Path, non_interactive: bool) -> None:
    """Check if the Modastack GitHub App is installed on this repo's org."""
    repo_full = _get_repo_full_name(path)
    if not repo_full:
        click.echo("  Could not detect GitHub remote — skipping app check")
        return

    owner = repo_full.split("/")[0]
    result = subprocess.run(
        ["gh", "api", f"orgs/{owner}/installations", "--jq",
         '[.installations[] | select(.app_slug == "modastack")] | length'],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip() not in ("", "0"):
        click.echo(f"  GitHub App installed on {owner}")
        return

    click.echo(f"  GitHub App not installed on {owner}")
    install_url = "https://github.com/apps/modastack/installations/new"
    if not non_interactive:
        click.echo(f"  Install at: {install_url}")
        try:
            if click.confirm("  Open in browser?", default=True):
                subprocess.run(["open", install_url], capture_output=True)
                click.pause("  Press Enter after installing...")
        except (EOFError, click.Abort):
            pass
    else:
        click.echo(f"  Install at: {install_url}")


def _ensure_event_server(path: Path, global_config: GlobalConfig) -> None:
    """Register a per-repo deployment with the event server.

    Each repo gets its own Cloudflare deployment with subscriptions
    scoped to that repo's GitHub, Linear, and Slack event sources.
    Credentials are stored in .modastack/local.yaml.
    """
    import httpx
    from .config import LocalConfig, RepoConfig

    local = LocalConfig.load(path)

    # Build subscription keys for this repo
    subscriptions: list[str] = []
    repo_full = _get_repo_full_name(path)
    if repo_full:
        subscriptions.append(repo_full)
    try:
        repo_config = RepoConfig.from_file(path)
        if repo_config.task_tracking == "linear" and repo_config.project:
            subscriptions.append(f"linear:{repo_config.project}")
        if repo_config.slack_workspace_id:
            subscriptions.append(f"slack:{repo_config.slack_workspace_id}")
    except FileNotFoundError:
        pass

    if not subscriptions:
        click.echo("  No event sources detected — skipping event server registration")
        return

    server_url = local.event_server_url or global_config.event_server_url or EVENT_SERVER_URL

    if local.event_server_deployment_id and local.event_server_api_key:
        # Existing per-repo deployment — update subscriptions
        try:
            resp = httpx.put(
                f"{server_url}/deployments/{local.event_server_deployment_id}/subscriptions",
                json={"add": subscriptions},
                headers={"Authorization": f"Bearer {local.event_server_api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                click.echo(f"  Event server: subscribed to {', '.join(subscriptions)} ({len(data['subscriptions'])} total)")
            else:
                click.echo(f"  Event server: failed to add subscription ({resp.status_code})")
        except Exception as e:
            click.echo(f"  Event server: failed to add subscription ({e})")
        return

    # New per-repo deployment
    click.echo(f"  Registering {path.name} with event server...")
    import socket
    hostname = socket.gethostname()
    try:
        resp = httpx.post(
            f"{server_url}/deployments",
            json={"name": f"{hostname}-{path.name}", "subscriptions": subscriptions},
            timeout=10,
        )
        if resp.status_code == 201:
            data = resp.json()
            local.event_server_url = server_url
            local.event_server_deployment_id = data["deployment_id"]
            local.event_server_api_key = data["api_key"]
            local.save(path)

            # Also store in global config for backward compat
            if not global_config.event_server_url:
                global_config.event_server_url = server_url
                global_config.event_server_deployment_id = data["deployment_id"]
                global_config.event_server_api_key = data["api_key"]
                global_config.save()

            click.echo(f"  Event server: registered ({', '.join(subscriptions)})")
        else:
            click.echo(f"  Event server registration failed: {resp.status_code} {resp.text}")
    except Exception as e:
        click.echo(f"  Event server registration failed: {e}")


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
    """List available workflow definitions from all sources.

    Scans three tiers in priority order (most specific wins):
      1. Repo-local: <repo>/.modastack/workflows/
      2. User: ~/.modastack/workflows/
      3. Built-in: <modastack>/workflows/

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
        MonitorRegistry.add_repo(m, repo_path)
        click.echo(f"Added monitor '{slug}' to {repo_path}/.modastack/monitors.yaml")
    else:
        MonitorRegistry.add_global(m)
        click.echo(f"Added global monitor '{slug}' to ~/.modastack/monitors.yaml")
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


@main.command()
@click.version_option(version=__version__, prog_name="modastack agent")
@click.option("--repo", default=None, help="Repo path or registered name")
@click.option("--workflow", "-w", required=True, help="Workflow to run (e.g. issue-lifecycle, adhoc)")
@click.option("--task", default=None, help="Task description / context for the engineer")
@click.option("--timeout", default=3600, type=int, help="Timeout in seconds")
@click.option("--wait", is_flag=True, help="Block until the agent completes")
@click.option("--post-event", "post_event", default=None,
              help="Post this event type on completion (for --wait checks)")
@click.option("--requested-by", "requested_by", default=None,
              help='JSON identity of requester, e.g. \'{"from":"Alice","channel":"C1"}\'')
@click.option("--non-interactive", "non_interactive", is_flag=True,
              help="Run without manager — agent makes all decisions autonomously")
def agent(repo, workflow, task, timeout, wait, post_event, requested_by, non_interactive):
    """Launch an agent with a workflow.

    Every agent runs a workflow. Use 'adhoc' for open-ended tasks.

    Examples:
        modastack agent -w issue-lifecycle --repo jobtack --task "Work on #42"
        modastack agent -w adhoc --repo jobtack --task "Why is CI failing?"
        modastack agent -w adhoc --non-interactive --repo jobtack --task "Fix the bug"
    """
    _dispatch_agent(repo=repo, task=task, workflow=workflow,
                    timeout=timeout, wait=wait, post_event=post_event,
                    requested_by=requested_by,
                    interactive=not non_interactive)


@main.command(hidden=True)
@click.option("--repo", default=None)
@click.option("--task", required=True)
@click.option("--timeout", default=3600, type=int)
@click.option("--non-interactive", "--check", "non_interactive", is_flag=True)
@click.option("--post-event", "post_event", default=None)
@click.option("--requested-by", "requested_by", default=None)
def spawn(repo, task, timeout, non_interactive, post_event, requested_by):
    """Backwards-compatible alias — routes to adhoc workflow."""
    _dispatch_agent(repo=repo, task=task, workflow="adhoc",
                    timeout=timeout, wait=non_interactive, post_event=post_event,
                    requested_by=requested_by)


def _dispatch_agent(*, repo, task, workflow, timeout, wait, post_event, requested_by, interactive=True):
    """Shared dispatch logic for the agent and spawn commands."""
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
        req = urllib.request.Request(
            "http://localhost:8095/api/event",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return bool(result.get("ok"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        logging.getLogger(__name__).warning(f"Failed to post event {event_type}: {e}")
        return False


@main.command("self-update")
@click.option("--no-restart", is_flag=True, help="Skip automatic restart after update")
def self_update(no_restart):
    """Pull latest from origin/main, reinstall, and restart.

    The restart happens via systemd (or a detached process) so the
    update completes cleanly even when called from inside the manager.
    """
    log = logging.getLogger(__name__)

    old_version = (REPO_ROOT / "VERSION").read_text().strip()
    click.echo(f"Current version: {old_version}")

    # Fetch latest
    click.echo("Fetching origin/main...")
    result = subprocess.run(
        ["git", "fetch", "origin", "main", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Failed to fetch: {result.stderr.strip()}", err=True)
        sys.exit(1)

    # Check if there are new commits
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()

    if local_head == remote_head:
        click.echo("Already up to date.")
        return

    # Check for dirty working tree — reset to remote instead of stashing
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()

    if dirty:
        click.echo("Working tree has uncommitted changes — resetting to origin/main...")
        subprocess.run(
            ["git", "stash", "push", "-m", "modastack-self-update-backup"],
            cwd=REPO_ROOT, check=True,
        )

    # Save rollback state
    import datetime
    UPDATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_STATE_PATH.write_text(json.dumps({
        "pre_update_head": local_head,
        "pre_update_version": old_version,
        "updated_at": datetime.datetime.now().isoformat(),
    }))

    # Reset to remote HEAD (avoids merge conflicts from SCP'd files)
    remote_version = subprocess.run(
        ["git", "show", "origin/main:VERSION"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip() or old_version
    commit_count = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.strip()
    click.echo(f"Updating {old_version} → {remote_version} ({commit_count} commit(s))...")
    result = subprocess.run(
        ["git", "reset", "--hard", "origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Reset failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    # Reinstall
    click.echo("Reinstalling...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"pip install failed: {result.stderr.strip()}", err=True)
        click.echo("Run `modastack rollback` to restore.")
        sys.exit(1)

    # Verify the wrapper is a valid Python script (not a shell self-loop)
    wrapper = Path(REPO_ROOT) / ".venv" / "bin" / "modastack"
    if wrapper.exists():
        first_line = wrapper.read_text().splitlines()[0]
        if "python" not in first_line:
            click.echo(f"WARNING: wrapper script looks corrupted ({first_line})", err=True)
            click.echo("Reinstalling to fix...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".", "--force-reinstall", "--quiet"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )

    new_version = (REPO_ROOT / "VERSION").read_text().strip()
    click.echo(f"Updated to v{new_version} ({remote_head[:8]})")
    log.info(f"Self-update complete: {old_version} → {new_version}")

    if no_restart:
        click.echo("Skipping restart (--no-restart). Run `modastack restart` manually.")
        return

    # Restart via systemd or detached process — never from inside our own process
    if _has_systemd_service():
        click.echo("Restarting via systemd...")
        subprocess.Popen(
            ["systemctl", "--user", "restart", "modastack"],
            start_new_session=True,
        )
        click.echo("Restart queued. The manager will be back shortly.")
    else:
        click.echo("Restarting...")
        subprocess.Popen(
            [sys.executable, "-m", "modastack.cli", "restart"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        click.echo("Restart queued.")


@main.command()
def rollback():
    """Roll back the last self-update."""
    if not UPDATE_STATE_PATH.exists():
        click.echo("No update state found — nothing to roll back.")
        sys.exit(1)

    state = json.loads(UPDATE_STATE_PATH.read_text())
    pre_head = state["pre_update_head"]
    pre_version = state["pre_update_version"]

    click.echo(f"Rolling back to v{pre_version} (commit {pre_head[:8]})...")

    result = subprocess.run(
        ["git", "reset", "--hard", pre_head],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"Reset failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"pip install failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    UPDATE_STATE_PATH.unlink(missing_ok=True)
    click.echo(f"Rolled back to v{pre_version}")


@main.command("migrate-worktrees")
def migrate_worktrees():
    """Move existing worktrees from <repo>/worktrees/ into modastack/worktrees/<repo>/."""
    config = GlobalConfig.load()
    modastack_root = REPO_ROOT
    moved = 0

    for repo_path in config.repos:
        old_wt_dir = repo_path / "worktrees"
        if not old_wt_dir.exists():
            continue

        repo_name = repo_path.name
        new_wt_dir = modastack_root / "worktrees" / repo_name
        new_wt_dir.mkdir(parents=True, exist_ok=True)

        for entry in old_wt_dir.iterdir():
            if not entry.is_dir():
                continue
            dest = new_wt_dir / entry.name
            if dest.exists():
                click.echo(f"  skip {entry.name} (already exists at {dest})")
                continue

            # Remove old worktree via git and re-add at new location
            branch_result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, cwd=repo_path,
            )
            # Find the branch for this worktree
            branch = None
            lines = branch_result.stdout.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("worktree ") and line.endswith(str(entry)):
                    for j in range(i + 1, min(i + 5, len(lines))):
                        if lines[j].startswith("branch "):
                            branch = lines[j].split("refs/heads/", 1)[-1]
                            break
                    break

            # Remove old worktree
            subprocess.run(
                ["git", "worktree", "remove", str(entry), "--force"],
                capture_output=True, text=True, cwd=repo_path,
            )

            # Re-add at new location
            if branch:
                subprocess.run(
                    ["git", "worktree", "add", str(dest), branch],
                    capture_output=True, text=True, cwd=repo_path,
                )
                click.echo(f"  moved {entry.name} → {dest}")
                moved += 1
            else:
                click.echo(f"  warn: could not determine branch for {entry.name}, removed only")

        # Clean up empty old worktrees dir
        if old_wt_dir.exists() and not any(old_wt_dir.iterdir()):
            old_wt_dir.rmdir()

    if moved == 0:
        click.echo("No worktrees to migrate.")
    else:
        click.echo(f"Migrated {moved} worktree(s).")


@main.command()
@click.option("--path", default=None, type=click.Path(),
              help="Install location (default: ~/.local/bin/modastack)")
@click.option("--uninstall", is_flag=True, help="Remove the global wrapper")
def install(path, uninstall):
    """Install modastack as a global command (no venv activation needed).

    Creates a small wrapper script that calls into the virtualenv's Python
    directly, so `modastack` works from any shell without sourcing .venv.

    Usage:
        modastack install                          # install to ~/.local/bin
        modastack install --path /usr/local/bin     # install to /usr/local/bin
        modastack install --uninstall               # remove wrapper
    """
    if path:
        target_dir = Path(path).expanduser().resolve()
        target = target_dir / "modastack" if target_dir.is_dir() else target_dir
    else:
        target = Path.home() / ".local" / "bin" / "modastack"

    if uninstall:
        if target.exists():
            target.unlink()
            click.echo(f"Removed {target}")
        else:
            click.echo(f"No wrapper at {target}")
        return

    venv_bin = Path(sys.prefix) / "bin" / "modastack"
    if not venv_bin.exists():
        venv_bin = Path(sys.prefix) / "bin" / "python"
    wrapper = f"#!/bin/sh\nexec {venv_bin} \"$@\"\n" if venv_bin.name == "modastack" else f"#!/bin/sh\nexec {venv_bin} -m modastack.cli \"$@\"\n"

    if target.exists():
        existing = target.read_text()
        if existing == wrapper:
            click.echo(f"Already installed: {target}")
            return
        click.echo(f"Updating: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(wrapper)
    target.chmod(0o755)
    click.echo(f"Installed: {target}")

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(target.parent) not in path_dirs:
        click.echo(f"\nAdd to your shell profile:")
        click.echo(f'  export PATH="{target.parent}:$PATH"')


if __name__ == "__main__":
    main()
