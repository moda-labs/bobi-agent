"""Event consumer — thin orchestrator.

Three components:
1. Manager session (Claude Code via Agent SDK)
2. Event client (WebSocket to event server → pushes to queue)
3. Drain loop (batches queued events → injects into manager)

The manager handles all response routing (Slack replies, etc.) using
its own tools. The consumer never touches transport-specific logic.
"""

import json
import logging
import os
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from modastack.config import GLOBAL_CONFIG_DIR
from modastack.manager.events.slack_responder import _markdown_to_slack
from modastack.manager.session import (
    ManagerSession, set_default_session,
    start_or_resume, is_alive, detect_state,
)

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
DRAIN_INTERVAL = 2
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"


def _cleanup_pid():
    PID_PATH.unlink(missing_ok=True)


def _post_dm(token: str, channel: str, text: str) -> None:
    """Post a manager text response to the configured Slack DM channel."""
    if not text.strip():
        return
    text = _markdown_to_slack(text)
    payload = json.dumps({"channel": channel, "text": text}).encode()
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            log.info(f"Slack reply sent to {channel}")
        else:
            log.warning(f"Slack DM error: {result.get('error')}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning(f"Slack DM failed: {e}")



def _wait_for_manager(timeout: int = 300) -> bool:
    """Block until the manager is in waiting_input state."""
    from modastack.manager.session import detect_state
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if detect_state() == "waiting_input":
            return True
        time.sleep(2)
    return False


def _drain_loop():
    """Drain the event queue and inject batched events into the manager."""
    from .event_client import event_queue, format_event_for_manager
    from modastack.manager.session import inject, detect_state, set_response_callback

    log.info("Drain loop waiting for manager to finish startup")
    if not _wait_for_manager():
        log.error("Manager never became ready — drain loop exiting")
        return
    log.info("Manager ready — drain loop active")

    from modastack.manager.session import get_default_session
    session = get_default_session()
    if session:
        from modastack.config import LocalConfig
        local = LocalConfig.load(session.repo_path)
        dm_token = local.slack_bot_token
        dm_channel = local.slack_dm_channel
    else:
        dm_token = ""
        dm_channel = ""
    if dm_channel and dm_token:
        set_response_callback(lambda t: _post_dm(dm_token, dm_channel, t))
        log.info(f"Streaming all manager output to Slack DM {dm_channel}")

    while True:
        event = event_queue.get()

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())

        slack_events = [e for e in batch if e.get("source") == "slack"]
        other_events = [e for e in batch if e.get("source") != "slack"]

        for group, is_slack in [(other_events, False), (slack_events, True)]:
            if not group:
                continue

            lines = [format_event_for_manager(e) for e in group]
            text = "\n\n".join(lines)

            if detect_state() != "waiting_input":
                log.info(f"Manager busy — waiting before injecting {len(group)} event(s)")
                if not _wait_for_manager():
                    log.warning(f"Manager not ready after wait — dropping {len(group)} event(s)")
                    continue

            log.info(f"Injecting {len(group)} event(s)")
            inject(text)


def _kill_stale_instances():
    """Kill any running modastack start processes besides ourselves."""
    import subprocess as sp
    import signal as sig
    my_pid = os.getpid()

    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
            if old_pid != my_pid:
                os.kill(old_pid, sig.SIGTERM)
                log.info(f"Killed stale instance from PID file (pid {old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass

    try:
        result = sp.run(
            ["pgrep", "-f", "modastack.*start"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                if pid != my_pid:
                    os.kill(pid, sig.SIGTERM)
                    log.info(f"Killed orphaned modastack process {pid}")
            except (ProcessLookupError, ValueError, PermissionError):
                pass
    except (FileNotFoundError, sp.TimeoutExpired):
        pass


def _build_subscriptions(repo_path: Path) -> list[str]:
    """Build subscription keys from repo config for event server registration."""
    subs: list[str] = []
    try:
        from modastack.config import RepoConfig
        rc = RepoConfig.from_file(repo_path)
        if rc.github_repo:
            subs.append(rc.github_repo)
        if rc.slack_workspace_id:
            subs.append(f"slack:{rc.slack_workspace_id}")
        if rc.project and rc.task_tracking == "linear":
            subs.append(f"linear:{rc.project}")
    except (FileNotFoundError, Exception) as e:
        log.warning(f"Could not read repo config for subscriptions: {e}")
    if not subs:
        # Fallback: use the repo directory name
        subs.append(repo_path.name)
    return subs


def run(repo_path: Path | None = None, **kwargs):
    """Start modastack for a single repo."""
    import atexit
    import signal
    from modastack.config import LocalConfig

    if repo_path is None:
        raise RuntimeError("repo_path is required — run from a repo with .modastack/config.yaml")

    from modastack.sdk import set_repo_root
    set_repo_root(repo_path)

    local = LocalConfig.load(repo_path)
    state_dir = repo_path / ".modastack" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Modastack starting for {repo_path.name}")
    _kill_stale_instances()

    # Write PID to both per-repo state dir and legacy path
    pid_str = str(os.getpid())
    (state_dir / "manager.pid").write_text(pid_str)
    PID_PATH.write_text(pid_str)

    def _cleanup():
        pid_file = state_dir / "manager.pid"
        try:
            if pid_file.exists() and pid_file.read_text().strip() == pid_str:
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        (state_dir / "dashboard.port").unlink(missing_ok=True)
        _cleanup_pid()
    atexit.register(_cleanup)

    def _handle_term(signum, frame):
        log.info("Received SIGTERM — shutting down")
        _cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)

    session = ManagerSession(repo_path=repo_path)
    set_default_session(session)

    if not session.start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info(f"Manager session '{session.session_name}' started")

    from modastack.workflow.triggers import WorkflowDispatcher
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    log.info(f"Loaded {len(dispatcher.workflows)} workflow(s)")

    # Event server — all config from per-repo local.yaml
    es_url = local.event_server_url
    es_deployment = local.event_server_deployment_id
    es_key = local.event_server_api_key

    event_client = None
    if es_url and es_key:
        from .event_client import EventServerClient
        event_client = EventServerClient(
            server_url=es_url,
            deployment_id=es_deployment,
            api_key=es_key,
        )
        event_client.start()
        log.info(f"Event client started -> {es_url}")
        atexit.register(event_client.stop)
    else:
        from .event_server import ensure_running, register

        es_port = 8080
        base_url = f"http://localhost:{es_port}"

        ensure_running(es_port)

        subs = _build_subscriptions(repo_path)
        deployment_id, api_key = register(base_url, repo_path.name, subs)

        from .event_client import EventServerClient
        event_client = EventServerClient(
            server_url=base_url,
            deployment_id=deployment_id,
            api_key=api_key,
        )
        event_client.start()
        log.info(f"Event client started -> {base_url} (local, auto-registered)")
        atexit.register(event_client.stop)

        log.info("Webhook endpoints ready:")
        log.info(f"  GitHub:  {base_url}/webhooks/github")
        log.info(f"  Linear:  {base_url}/webhooks/linear")
        log.info(f"  Slack:   {base_url}/webhooks/slack")

    # Start drain loop
    drain_thread = threading.Thread(target=_drain_loop, daemon=True, name="drain-loop")
    drain_thread.start()

    from modastack.monitors.scheduler import MonitorScheduler
    monitor_scheduler = MonitorScheduler()
    monitor_scheduler.start()

    # Dashboard — use port from local config, write chosen port to state
    dashboard_port = local.dashboard_port or 8095
    from dashboard.app import run_dashboard
    dashboard_thread = threading.Thread(
        target=run_dashboard, kwargs={"port": dashboard_port},
        daemon=True, name="dashboard",
    )
    dashboard_thread.start()
    (state_dir / "dashboard.port").write_text(str(dashboard_port))
    log.info(f"Dashboard started on http://localhost:{dashboard_port}")

    dm_token = local.slack_bot_token
    dm_channel = local.slack_dm_channel
    if dm_token and dm_channel:
        _post_dm(dm_token, dm_channel, f"Modastack started for {repo_path.name}.")

    log.info(f"Modastack running for {repo_path.name}")

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not session.is_alive():
            log.warning("Manager session died — restarting")
            session.start_or_resume()
