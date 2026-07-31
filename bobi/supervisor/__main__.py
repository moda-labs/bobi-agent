"""Supervisor sidecar entrypoint (issue #5).

Two ways in, one body:

    bobi agent <name> supervise -- --foreground "$@"   # the CLI surface (D1)
    BOBI_ROOT=<run-root> python -m bobi.supervisor -- --foreground "$@"

Everything after ``--`` is forwarded verbatim to the selected agent's start
command (``--foreground`` keeps the manager a supervisable child). It spawns +
supervises the manager exactly as ``bobi supervise`` did (the ported watchdog),
and attaches the telemetry observer that publishes tier-1 heartbeat + tier-2
lifecycle events onto the bus.

The two entry points differ only in how the run root is chosen: the CLI has
already bound it from the agent name, while ``python -m`` resolves it from
``BOBI_ROOT``. ``run()`` takes the resolved root so neither path re-derives
the other's answer.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .admin import AdminListener
from .alerting import SlackAlerter
from .config import SupervisorConfig
from .supervision import CompositeObserver, Supervisor
from .telemetry import Telemetry

log = logging.getLogger("bobi.supervisor")


def _resolve_run_root() -> Path:
    """Bind this process to the run root the entrypoint selected."""
    from bobi import paths
    raw = os.environ.get("BOBI_ROOT")
    root = Path(raw).resolve() if raw else (paths.home_dir() / "run")
    paths.bind_root(root)
    return root


def _configure_logging() -> None:
    # Container/foreground mode: log to stdout/stderr only (mirror `bobi start`
    # / `bobi supervise`). Strip any file handlers a library may have added.
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers
                     if not isinstance(h, logging.FileHandler)]
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m bobi.supervisor`` entry: resolve the root from the
    environment, then run."""
    argv = list(sys.argv[1:] if argv is None else argv)
    _configure_logging()
    return run(_resolve_run_root(), argv)


def run(root: Path, argv: list[str]) -> int:
    """Supervise the manager under an ALREADY-BOUND run *root*.

    The CLI (``bobi agent <name> supervise``) binds the root from the agent
    name before calling this; ``main()`` binds it from ``BOBI_ROOT``. Passing
    it in keeps that the only difference between the two entry points.
    """
    # click consumes the `--` separator when the CLI parses it; strip any that
    # survives so `... supervise -- --foreground` and `... supervise
    # --foreground` behave identically on both paths.
    start_args = [a for a in argv if a != "--"]

    config = SupervisorConfig.from_env()

    telemetry = Telemetry(project_root=root, config=config)
    # Best-effort JOIN of the instance bubble so publishes are signed. The
    # manager mints it on first registration; we converge on the same bubble.
    telemetry.join_bubble()

    log.info("supervisor: supervising manager start %s (fleet=%s instance=%s "
             "platform=%s)", " ".join(start_args),
             telemetry.identity.get("fleet"), telemetry.identity.get("instance"),
             telemetry.identity.get("platform"))

    # Incident alerting (#4): a second observer on the same lifecycle edges.
    # Its lifecycle_fn feeds incident edges back onto the tier-2 trail; its
    # announce_fallback keeps budget exhaustion single-posted (the rich alert
    # fires from the budget_exhausted event, the ported plain message only if
    # that could not post).
    alerter = SlackAlerter(project_root=root, config=config,
                           identity=telemetry.identity,
                           lifecycle_fn=telemetry.lifecycle)
    supervisor = Supervisor(start_args, config, project_root=root,
                            observer=CompositeObserver([telemetry, alerter]),
                            announce_fn=alerter.announce_fallback)

    # The inbound half (#9): listen for operator admin commands on this
    # deployment's admin topic and run them against the supervisor. Its own
    # bus connection in the same bubble, independent of the manager - so a
    # restart works even when the manager is wedged. Fail-open: if it cannot
    # start, the supervisor still supervises.
    admin = AdminListener(supervisor=supervisor, telemetry=telemetry,
                          project_root=root)
    admin.start()
    try:
        return supervisor.run()
    finally:
        admin.stop()


if __name__ == "__main__":
    raise SystemExit(main())
