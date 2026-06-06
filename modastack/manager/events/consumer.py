"""Event consumer — project lead orchestrator.

Three components:
1. Lead session (Claude Code via Agent SDK)
2. Event client (WebSocket to event server → pushes to queue)
3. Drain loop (batches queued events → injects into lead)

The lead handles event processing, workflow dispatch, and agent
management. All human communication goes through the assistant.
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

from modastack.manager.session import (
    ManagerSession, set_default_session,
)

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
DRAIN_INTERVAL = 2



def _drain_loop(manager_session_name: str):
    """Event bus sidecar — batches events and delivers to the manager's inbox."""
    from .event_client import event_queue, format_event_for_manager
    from modastack.inbox import deliver

    log.info("Drain loop active — delivering events to manager inbox")

    while True:
        event = event_queue.get()

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())

        slack_events = [e for e in batch if e.get("source") == "slack"]
        other_events = [e for e in batch if e.get("source") != "slack"]

        for group in [other_events, slack_events]:
            if not group:
                continue

            lines = [format_event_for_manager(e) for e in group]
            text = "\n\n".join(lines)

            log.info(f"Delivering {len(group)} event(s) to {manager_session_name}")
            ok, _ = deliver(manager_session_name, text, sender="event-bus")
            if not ok:
                log.warning(f"Event delivery failed for {len(group)} event(s)")


def _kill_stale_instances(project_path: Path):
    """Kill any previous modastack manager for THIS project only.

    Only uses the project's own PID file — never scans for other modastack
    processes, since multiple projects run independent managers.
    """
    import signal as sig
    my_pid = os.getpid()

    pid_file = project_path / ".modastack" / "state" / "manager.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != my_pid:
                os.kill(old_pid, sig.SIGTERM)
                log.info(f"Killed stale instance from PID file (pid {old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass


def _build_subscriptions(project_path: Path) -> list[str]:
    """Build subscription keys from project config for event server registration."""
    subs: list[str] = []
    try:
        from modastack.config import ProjectConfig
        pc = ProjectConfig.from_file(project_path)
        if pc.github_repo:
            subs.append(pc.github_repo)
        if pc.slack_workspace_id and pc.slack_channel:
            subs.append(f"slack:{pc.slack_workspace_id}:{pc.slack_channel}")
        elif pc.slack_workspace_id:
            log.warning("slack.workspace_id set but no slack.channel — "
                        "Slack events will not be routed to this manager. "
                        "Set slack.channel in .modastack/config.yaml.")
        if pc.linear_team and pc.task_tracking == "linear":
            subs.append(f"linear:{pc.linear_team}")
    except (FileNotFoundError, Exception) as e:
        log.warning(f"Could not read project config for subscriptions: {e}")
    if not subs:
        subs.append(project_path.name)
    return subs


def run(project_path: Path | None = None, **kwargs):
    """Start modastack for a single project."""
    import atexit
    import signal
    from modastack.config import LocalConfig

    if project_path is None:
        raise RuntimeError("project_path is required — run from a project with .modastack/config.yaml")

    from modastack.sdk import set_project_root
    set_project_root(project_path)

    local = LocalConfig.load(project_path)
    state_dir = project_path / ".modastack" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Modastack starting for {project_path.name}")
    _kill_stale_instances(project_path)

    pid_str = str(os.getpid())
    (state_dir / "manager.pid").write_text(pid_str)

    def _cleanup():
        pid_file = state_dir / "manager.pid"
        try:
            if pid_file.exists() and pid_file.read_text().strip() == pid_str:
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        (state_dir / "dashboard.port").unlink(missing_ok=True)
    atexit.register(_cleanup)

    def _handle_term(signum, frame):
        log.info("Received SIGTERM — shutting down")
        _cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)

    session = ManagerSession(project_path=project_path)
    set_default_session(session)

    if not session.start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info(f"Manager session '{session.session_name}' started")

    from modastack.workflow.triggers import WorkflowDispatcher
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    log.info(f"Loaded {len(dispatcher.workflows)} workflow(s)")

    # Event server — URL from project config, credentials from local config
    from modastack.config import ProjectConfig
    project_config = ProjectConfig.from_file(project_path)
    es_url = project_config.event_server_url
    es_deployment = local.event_server_deployment_id
    es_key = local.event_server_api_key

    event_client = None
    if es_url and es_key:
        # Verify saved credentials still work (event server may have restarted)
        valid = False
        try:
            import json as _json, urllib.request
            subs = _build_subscriptions(project_path)
            req = urllib.request.Request(
                f"{es_url}/deployments/{es_deployment}/subscriptions",
                data=_json.dumps({"add": subs}).encode(),
                headers={
                    "Authorization": f"Bearer {es_key}",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=3):
                valid = True
        except Exception:
            pass

        if not valid and es_url.startswith("http://localhost"):
            log.info("Saved event server credentials are stale — re-registering")
            from .event_server import ensure_running, register
            es_port = int(es_url.rsplit(":", 1)[-1].rstrip("/"))
            ensure_running(es_port, project_path=project_path)
            subs = _build_subscriptions(project_path)
            es_deployment, es_key = register(es_url, project_path.name, subs)

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

        ensure_running(es_port, project_path=project_path)

        subs = _build_subscriptions(project_path)
        deployment_id, api_key = register(base_url, project_path.name, subs)

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

    # Start drain loop (event bus sidecar)
    drain_thread = threading.Thread(
        target=_drain_loop, args=(session.session_name,),
        daemon=True, name="drain-loop",
    )
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

    log.info(f"Modastack running for {project_path.name}")

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not session.is_alive():
            log.warning("Manager session died — restarting")
            session.start_or_resume()
