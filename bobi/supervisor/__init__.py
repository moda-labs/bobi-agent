"""Supervisor sidecar - the deployment's terminal process (issue #5).

Spawns and supervises the manager, probes it from outside, and publishes
tier-1 heartbeat + tier-2 lifecycle telemetry onto the event bus. It also
listens on the deployment's admin topic, which is what lets an operator
restart a manager that has wedged - the reason the sidecar is a separate
process rather than a thread inside the thing it supervises.

Run it as ``bobi agent <name> supervise`` (the CLI binds the runtime root and
forwards everything after ``--`` to the manager's start command), or as
``python -m bobi.supervisor`` where a container entrypoint wants the module
directly.

Dependency discipline: this package imports only stdlib plus the same narrow
public ``bobi`` surfaces it always did (``bobi.paths``, ``bobi.events``,
``bobi.sdk``, ``bobi.slack``). It was written to run against a venv holding
nothing but ``bobi``, and that constraint still earns its keep now that it
ships IN ``bobi`` - a sidecar that drags in the whole framework's optional
extras is one that fails to start for the reason it exists to report.
"""

from .config import SupervisorConfig
from .supervision import (
    EXIT_BUDGET_EXHAUSTED,
    RestartBudget,
    Supervisor,
    SupervisorObserver,
    SupervisorState,
    is_wedged,
)

__all__ = [
    "SupervisorConfig",
    "EXIT_BUDGET_EXHAUSTED",
    "RestartBudget",
    "Supervisor",
    "SupervisorObserver",
    "SupervisorState",
    "is_wedged",
]
